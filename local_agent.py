#!/usr/bin/env python3
"""
AMS Export Agent — Vision-map, Claude does all field matching.

How it works:
  1. Take ONE screenshot of the AMS window.
  2. Send the screenshot to the server, which forwards it to Claude.
  3. Claude figures out which data belongs in which visible field,
     returns coordinates + formatted values ready to paste.
  4. pyautogui bulk-fills everything — no more API calls.
  5. Scroll down, repeat if more fields are below the fold.

No field mappings to maintain. Works on any AMS, any layout.
Total API calls: 1 per screen-full (usually 1-2 for a full form).

Usage:
    python local_agent.py
    python local_agent.py --server http://192.168.1.100:5001
"""

import io
import json
import logging
import platform
import queue

# Windows DPI awareness — must be set before any GUI/window operations
# Without this, WindowFromPoint and screenshot coordinates are wrong on
# multi-monitor setups with display scaling above 100%.
if platform.system() == "Windows":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # Minimize the console window so it's not visible while the agent runs
    try:
        _hwnd_console = ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd_console:
            ctypes.windll.user32.ShowWindow(_hwnd_console, 6)  # SW_MINIMIZE
    except Exception:
        pass


def _close_console_window():
    """Close (hide) the console window when the agent is done."""
    if platform.system() == "Windows":
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        except Exception:
            pass

import re
import sys
import tempfile
import threading
import time
import uuid
import argparse
import base64
from pathlib import Path
from typing import Optional
from PIL.ImageOps import scale
import pyautogui
import pyperclip
import requests

try:
    import mss
    from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat
    USE_MSS = True
except ImportError:
    from PIL import ImageGrab, Image, ImageChops, ImageOps, ImageStat
    USE_MSS = False

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SERVER_URL = "http://localhost:5001"
AGENT_ID           = str(uuid.uuid4())[:8]
POLL_INTERVAL      = 0.5   # seconds between idle polls
MAX_SCROLL_PASSES  = 1     # max scroll passes per job (safety limit)
CLICK_DELAY        = 0.08  # seconds to wait after clicking a field
FILL_DELAY         = 0.06  # seconds between filling each field
VERIFY_DELAY       = 0.12  # seconds to wait before verifying a field
MAX_FIELD_RETRIES  = 3     # max attempts per field before giving up
RETRY_OFFSET_PX    = 3     # pixel nudge on retry attempts

# Crop the detected app window down to the form viewport so vision
# never sees browser chrome, tabs, or the address bar.
FORM_REGION_INSET_TOP    = 95
FORM_REGION_INSET_LEFT   = 18
FORM_REGION_INSET_RIGHT  = 18
FORM_REGION_INSET_BOTTOM = 18

IS_MAC        = platform.system() == "Darwin"
PASTE_HOTKEY  = ("command", "v") if IS_MAC else ("ctrl", "v")
SELECT_HOTKEY = ("command", "a") if IS_MAC else ("ctrl", "a")
COPY_HOTKEY   = ("command", "c") if IS_MAC else ("ctrl", "c")

# Remote mode: use typewrite instead of clipboard paste, skip verification
REMOTE_MODE = False
TYPEWRITE_INTERVAL = 0.05  # seconds between keystrokes in remote mode

# Debug output directory for fill verification screenshots
DEBUG_FILL_DIR = Path(__file__).parent / "logs" / "fill_screenshots"
DEBUG_FILL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=f"[Agent {AGENT_ID}] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.info(
    "Screenshot backend: "
    + ("mss (multi-monitor safe)" if USE_MSS else "PIL ImageGrab — run: pip install mss")
)

job_queue: queue.Queue = queue.Queue()
persistent_overlay = None  # Global persistent overlay widget


# ─────────────────────────────────────────────────────────────────────────────
# macOS Accessibility helpers — snap clicks to the nearest text field
# ─────────────────────────────────────────────────────────────────────────────

_AX_AVAILABLE = False
if IS_MAC:
    try:
        from ApplicationServices import (
            AXUIElementCopyElementAtPosition,
            AXUIElementCreateSystemWide,
            AXUIElementCopyAttributeValue,
            AXUIElementSetAttributeValue,
            AXUIElementPerformAction,
            kAXErrorSuccess,
        )
        from CoreFoundation import CFEqual
        _AX_SYSTEMWIDE = AXUIElementCreateSystemWide()
        _AX_AVAILABLE = True
    except ImportError as _ax_err:
        logger.warning(f"Accessibility API not available: {_ax_err}")


# Roles that accept text input
_AX_TEXT_ROLES = {
    "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox",
    "AXSecureTextField",
}


def ax_get(elem, attr: str):
    """Safely read an accessibility attribute. Returns None on failure."""
    try:
        err, value = AXUIElementCopyAttributeValue(elem, attr, None)
        if err == kAXErrorSuccess:
            return value
    except Exception:
        pass
    return None


def ax_element_at(x: int, y: int):
    """Get the AX element at screen position (x, y), or None."""
    if not _AX_AVAILABLE:
        return None
    try:
        err, elem = AXUIElementCopyElementAtPosition(_AX_SYSTEMWIDE, float(x), float(y), None)
        if err == kAXErrorSuccess and elem is not None:
            return elem
    except Exception as e:
        logger.debug(f"ax_element_at({x},{y}) failed: {e}")
    return None


def ax_role(elem) -> str:
    """Get the AXRole of an element as a string, or empty string."""
    role = ax_get(elem, "AXRole")
    return str(role) if role else ""


def ax_find_nearest_text_field(x: int, y: int, max_radius: int = 60):
    """
    Find the nearest text-input element to (x, y) by sampling points in a
    spiral pattern. Returns the AXUIElement or None.
    """
    if not _AX_AVAILABLE:
        return None

    # Try the exact point first
    elem = ax_element_at(x, y)
    if elem is not None and ax_role(elem) in _AX_TEXT_ROLES:
        return elem

    # Sample points in expanding rings
    for radius in (10, 20, 30, 40, max_radius):
        for dx, dy in [(0, radius), (0, -radius), (radius, 0), (-radius, 0),
                       (radius, radius), (-radius, -radius),
                       (radius, -radius), (-radius, radius)]:
            if abs(dx) > max_radius or abs(dy) > max_radius:
                continue
            elem = ax_element_at(x + dx, y + dy)
            if elem is not None and ax_role(elem) in _AX_TEXT_ROLES:
                logger.debug(f"  AX: found {ax_role(elem)} at offset ({dx},{dy})")
                return elem
    return None


def ax_set_value(elem, value: str) -> bool:
    """
    Try to set the value of a text-input element directly via Accessibility.
    Returns True on success, False otherwise.
    """
    if not _AX_AVAILABLE or elem is None:
        return False
    try:
        # Focus it first so the user/system sees the change
        AXUIElementSetAttributeValue(elem, "AXFocused", True)
        err = AXUIElementSetAttributeValue(elem, "AXValue", value)
        return err == kAXErrorSuccess
    except Exception as e:
        logger.debug(f"ax_set_value failed: {e}")
        return False


def ax_get_value(elem) -> str:
    """Read the current value of a text-input element."""
    if not _AX_AVAILABLE or elem is None:
        return ""
    val = ax_get(elem, "AXValue")
    return str(val) if val is not None else ""


def ax_fill_field(x: int, y: int, value: str) -> tuple[bool, str]:
    """
    Try to fill a text field near (x, y) using the Accessibility API.
    Returns (success, info) where info describes what happened.
    """
    if not _AX_AVAILABLE:
        return False, "ax_unavailable"

    elem = ax_find_nearest_text_field(x, y)
    if elem is None:
        return False, "no_field_found"

    role = ax_role(elem)
    if ax_set_value(elem, value):
        # Verify
        readback = ax_get_value(elem)
        if value in readback or readback == value:
            return True, f"ax_success ({role})"
        else:
            return False, f"ax_set_but_readback_mismatch (role={role}, got={readback[:30]!r})"
    return False, f"ax_set_failed (role={role})"


def extract_json(text: str) -> dict:
    """
    Robustly pull a JSON object out of Claude's response.
    Handles plain JSON, ```json fences, ``` fences, JSON buried in prose.
    Raises ValueError if nothing parseable is found.
    """
    text = text.strip()

    # 1. Plain JSON — Claude often returns this cleanly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced block: ```json { ... } ``` or ``` { ... } ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # 3. First { ... } block anywhere in the text
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON found in Claude response:\n{text[:400]}")

SCREENSHOT_SCALE = 0.5  # Reduce image size for faster upload/processing
JPEG_QUALITY = 65  # Lower = faster + smaller file, higher = better quality (1-100)

def take_screenshot(region: dict,marker: tuple = None) -> tuple[bytes, float]:
    """
    Capture the given screen region and return JPEG bytes.
    mss handles negative y coordinates correctly on macOS dual-monitor setups.
    Also saves a debug copy to /tmp/ams_debug_last.png for inspection.

    Speed optimizations:
    - Uses JPEG instead of PNG (much faster compression, 3-10x smaller files)
    - Configurable quality setting (JPEG_QUALITY)
    - Resize before encoding to reduce data size
    """
    if USE_MSS:
        with mss.mss() as sct:
            monitor = {
                "left":   region["x"],
                "top":    region["y"],
                "width":  region["width"],
                "height": region["height"],
            }
            shot = sct.grab(monitor)
            img  = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    else:
        bbox = (
            region["x"],
            region["y"],
            region["x"] + region["width"],
            region["y"] + region["height"],
        )
        img = ImageGrab.grab(bbox=bbox, all_screens=True)

    if marker:
        draw = ImageDraw.Draw(img)
        mx, my = marker
        # Red crosshair, 20px size
        print(f"Drawing marker at ({mx},{my})")
        draw.line([(mx-20, my), (mx+20, my)], fill="red", width=2)
        draw.line([(mx, my-20), (mx, my+20)], fill="red", width=2)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    debug_path = Path(tempfile.gettempdir()) / f"ams_debug_{timestamp}.png"
    img.save(str(debug_path))
    logger.info(f"Screenshot: {debug_path} ({img.width}x{img.height})")

    # Shrink before encoding (major speed boost)
    new_w = int(img.width  * SCREENSHOT_SCALE)
    new_h = int(img.height * SCREENSHOT_SCALE)
    img_small = img.resize((new_w, new_h), Image.LANCZOS)

    # Use JPEG instead of PNG - much faster and 3-10x smaller
    buf = io.BytesIO()
    img_small.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False)
    bytes_data = buf.getvalue()

    logger.info(f"Compressed to {len(bytes_data) / 1024:.1f}KB JPEG (quality={JPEG_QUALITY})")
    return bytes_data, SCREENSHOT_SCALE


def inset_region(region: dict,
                 top: int = 0,
                 left: int = 0,
                 right: int = 0,
                 bottom: int = 0) -> dict:
    """
    Return a smaller region inset from the given bounds.
    Keeps width/height at least 1px so downstream capture never explodes.
    """
    x = region["x"] + left
    y = region["y"] + top
    width = max(1, region["width"] - left - right)
    height = max(1, region["height"] - top - bottom)
    return {"x": x, "y": y, "width": width, "height": height}

def flatten_with_path(d, parent_key=""):
    items = []
    for k, v in d.items():
        path = f"{parent_key}.{k}" if parent_key else k
        if isinstance(v, dict) and "value" not in v:
            items.extend(flatten_with_path(v, path))
        else:
            items.append((path, v))
    return items

def flatten_job_data(json_data: dict) -> dict:
    """
    Collapse the nested quote JSON into a flat dict.
    Keys describe WHAT the data IS, not what the AMS calls it.
    Claude handles the label matching.
    """
    quotes  = json_data.get("quotes", [])
    quote   = quotes[0] if quotes else {}
    policy  = quote.get("policies", [{}])[0]
    insured = quote.get("insured", {})
    addr    = insured.get("address", {})

    flat = {
        # Who is being insured
        "insured legal name":           insured.get("name"),
        "insured street address":       addr.get("street"),
        "insured city":                 addr.get("city"),
        "insured state":                addr.get("state"),
        "insured zip":                  addr.get("zip"),

        # What is being insured
        "type of coverage":             policy.get("coverage_type"),
        "insurance carrier":            policy.get("carrier"),
        "policy number":                policy.get("policy_number"),
        "policy start date":            policy.get("effective_date"),
        "policy end date":              policy.get("expiration_date"),
        "annual premium amount":        policy.get("annual_premium"),

        # Who is selling it
        "retail agent or broker name":  quote.get("retail_agent", {}).get("name"),
        "wholesale broker name":        quote.get("general_agent_or_wholesale_broker", {}).get("name"),
        "retail agent phone":           quote.get("retail_agent", {}).get("phone"),

        # Totals
        "total premium including fees": quote.get("totals", {}).get("grand_total"),
        "taxes":                        quote.get("totals", {}).get("total_tax"),
        "fees":                         quote.get("totals", {}).get("total_fee"),
    }

    # Drop Nones — don't send empty keys to Claude
    return {k: v for k, v in flat.items() if v is not None}

def screenshots_almost_equal(first: bytes, second: bytes, threshold: float = 2.0) -> bool:
    """
    Compare screenshots with a small tolerance so focus rings/caret repaints
    do not force another AI pass.
    """
    first_img = Image.open(io.BytesIO(first)).convert("RGB")
    second_img = Image.open(io.BytesIO(second)).convert("RGB")

    if first_img.size != second_img.size:
        return False

    # Shrink and grayscale to ignore tiny pixel-level noise.
    target_size = (160, 100)
    first_small = ImageOps.grayscale(first_img.resize(target_size))
    second_small = ImageOps.grayscale(second_img.resize(target_size))
    diff = ImageChops.difference(first_small, second_small)
    mean_diff = ImageStat.Stat(diff).mean[0]
    logger.info(f"Screenshot diff score: {mean_diff:.2f}")
    return mean_diff <= threshold

def  run_vision_job(server_url: str, json_data: dict, region: dict, job_id: int = None) -> bool:
    all_filled: set = set()
    remaining_data  = flatten_job_data(json_data)   # starts full, shrinks each pass
    previous_pass_screenshot: Optional[bytes] = None

    for pass_num in range(MAX_SCROLL_PASSES):
        logger.info(f"--- Pass {pass_num + 1}/{MAX_SCROLL_PASSES} ---")
        logger.info(f"Remaining data keys: {list(remaining_data.keys())}")

        print(f"\n  Taking screenshot for claude's  pass {pass_num + 1} over form...")
        current_ss, scale = take_screenshot(region)
        if previous_pass_screenshot is not None and screenshots_almost_equal(
            previous_pass_screenshot,
            current_ss,
        ):
            logger.info("Screen is unchanged from the prior pass — stopping.")
            break

        tb_data_map = get_tb_coords(server_url, current_ss, remaining_data, all_filled, job_id=job_id)
        safe_click  = tb_data_map.pop("__safe_click__", None)
        logger.info(f"filling")
        newly_filled = tb_fill(tb_data_map, region, scale, job_id=job_id)
        all_filled.update(newly_filled)

        # COMMENTED OUT: Remove the data values that got placed this pass
        # This keeps the data persistent so the same data can fill multiple forms
        # for label, info in tb_data_map.items():
        #     if label.startswith("__") or label not in newly_filled:
        #         continue
        #     key_path = info.get("key_path")
        #     if key_path and key_path in remaining_data:
        #         logger.info(f"Removing '{key_path}' from remaining data")
        #         del remaining_data[key_path]

        # COMMENTED OUT: Also remove matched non-text fields so we do not keep re-proposing
        # dropdowns/selects in later textbox-only passes.
        # for label, info in tb_data_map.items():
        #     if label.startswith("__") or not isinstance(info, dict):
        #         continue
        #     if label in newly_filled:
        #         continue
        #     if info.get("field_type") == "text_field":
        #         continue

        #     key_path = info.get("key_path")
        #     if key_path and key_path in remaining_data:
        #         logger.info(
        #             f"Removing matched non-text field '{key_path}' "
        #             f"(label='{label}', field_type='{info.get('field_type')}') from remaining data"
        #         )
        #         del remaining_data[key_path]

        # COMMENTED OUT: Early exit when data is empty - keep data persistent
        # if not remaining_data:
        #     logger.info("json quote data empty — done")
        #     break

        previous_pass_screenshot = current_ss
        # print(f"  Scrolling down for next pass...")
        # scroll_form(region, safe_click=safe_click)

    logger.info(f"Done. Filled: {sorted(all_filled)}")
    return len(all_filled) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Computer Use Mode — Agentic loop where Claude controls the mouse/keyboard
# ─────────────────────────────────────────────────────────────────────────────

COMPUTER_USE_TIMEOUT = 45  # seconds max for the agentic loop
COMPUTER_USE_MAX_STEPS = 30  # max actions before stopping

def run_computer_use_job(server_url: str, region: dict, job_id: int = None) -> bool:
    """
    Agentic computer-use loop:
    1. Take screenshot of the AMS form
    2. Send to server (which calls Claude with computer-use tool)
    3. Get back an action (click, type, scroll, etc.)
    4. Execute the action via pyautogui
    5. Repeat until Claude says done or timeout
    """
    messages = []  # conversation history maintained server-side
    start_time = time.time()
    actions_taken = 0

    logger.info(f"[Computer Use] Starting agentic loop for job {job_id}")
    logger.info(f"[Computer Use] Region: {region}")
    logger.info(f"[Computer Use] Timeout: {COMPUTER_USE_TIMEOUT}s, Max steps: {COMPUTER_USE_MAX_STEPS}")

    while True:
        # Check timeout
        elapsed = time.time() - start_time
        if elapsed > COMPUTER_USE_TIMEOUT:
            logger.info(f"[Computer Use] Timeout reached ({elapsed:.1f}s). Stopping.")
            break

        # Check step limit
        if actions_taken >= COMPUTER_USE_MAX_STEPS:
            logger.info(f"[Computer Use] Max steps reached ({actions_taken}). Stopping.")
            break

        # 1. Take screenshot — resize to 1280x720 for API (per Anthropic best practices)
        CU_DISPLAY_W, CU_DISPLAY_H = 1280, 720
        screenshot_bytes, _raw_scale = take_screenshot(region)
        # Resize to 1280x720 regardless of native size
        from PIL import Image as _PILImage
        _ss_img = _PILImage.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        _ss_resized = _ss_img.resize((CU_DISPLAY_W, CU_DISPLAY_H), _PILImage.LANCZOS)
        _ss_buf = io.BytesIO()
        _ss_resized.save(_ss_buf, format="JPEG", quality=80)
        screenshot_bytes = _ss_buf.getvalue()
        # Scale factors: Claude coords (1280x720) → native region pixels
        scale_x = region['width'] / CU_DISPLAY_W
        scale_y = region['height'] / CU_DISPLAY_H
        logger.info(f"[Computer Use] Step {actions_taken + 1}: screenshot taken ({CU_DISPLAY_W}x{CU_DISPLAY_H})")

        # 2. Send to server
        payload = {
            'screenshot': base64.b64encode(screenshot_bytes).decode('ascii'),
            'job_id': job_id,
            'messages': messages,
            'display_width': CU_DISPLAY_W,
            'display_height': CU_DISPLAY_H,
        }

        try:
            response = requests.post(
                f"{server_url}/api/ams/computer-use-step",
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"[Computer Use] Server request failed: {e}")
            break

        if not data.get('success'):
            logger.error(f"[Computer Use] Server error: {data.get('error')}")
            break

        # Update conversation history
        messages = data.get('messages', [])
        actions = data.get('actions', [])
        is_done = data.get('is_done', False)
        assistant_text = data.get('assistant_text', '')

        if assistant_text:
            logger.info(f"[Computer Use] Claude says: {assistant_text[:100]}")

        if is_done:
            logger.info(f"[Computer Use] Claude indicated done. Total actions: {actions_taken}")
            return True

        if not actions:
            # Claude responded without tool_use — task is complete
            logger.info(f"[Computer Use] No more actions — task complete. Total actions: {actions_taken}")
            return actions_taken > 0

        # 3. Execute each action
        for action in actions:
            action_type = action.get('action', '')
            logger.info(f"[Computer Use] Executing: {action_type} {action}")

            try:
                _execute_computer_action(action, region, scale_x, scale_y)
                actions_taken += 1
            except Exception as e:
                logger.error(f"[Computer Use] Action failed: {e}")

            time.sleep(0.1)  # small delay between actions

        # Small delay before next screenshot to let the UI update
        time.sleep(0.3)

    logger.info(f"[Computer Use] Loop ended. {actions_taken} actions taken in {time.time() - start_time:.1f}s")
    return actions_taken > 0


def _execute_computer_action(action: dict, region: dict, scale_x: float, scale_y: float):
    """Execute a single computer-use action from Claude.
    scale_x/scale_y convert from Claude's 1280x720 coords to native region pixels."""
    action_type = action.get('action', '')
    coordinate = action.get('coordinate')  # [x, y] in 1280x720 space

    if action_type == 'screenshot':
        # Claude just wants a new screenshot — we'll take one next iteration
        return

    elif action_type in ('left_click', 'click'):
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.click(abs_x, abs_y)
            logger.info(f"  → click at ({abs_x}, {abs_y})")
            time.sleep(CLICK_DELAY)

    elif action_type == 'double_click':
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.doubleClick(abs_x, abs_y)
            logger.info(f"  → double_click at ({abs_x}, {abs_y})")
            time.sleep(CLICK_DELAY)

    elif action_type == 'triple_click':
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.click(abs_x, abs_y, clicks=3)
            logger.info(f"  → triple_click at ({abs_x}, {abs_y})")
            time.sleep(CLICK_DELAY)

    elif action_type == 'right_click':
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.rightClick(abs_x, abs_y)
            time.sleep(CLICK_DELAY)

    elif action_type == 'type':
        text = action.get('text', '')
        if text:
            pyperclip.copy(text)
            pyautogui.hotkey(*PASTE_HOTKEY)
            logger.info(f"  → type (paste): '{text[:50]}'")
            time.sleep(0.05)

    elif action_type == 'key':
        key = action.get('text', '')
        if key:
            # Handle special keys and combos
            if '+' in key:
                keys = [k.strip().lower() for k in key.split('+')]
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(key.lower())
            logger.info(f"  → key: {key}")

    elif action_type == 'scroll':
        direction = action.get('scroll_direction', action.get('direction', 'down'))
        amount = action.get('scroll_amount', action.get('amount', 5))
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.click(abs_x, abs_y)  # Focus the area first
            time.sleep(0.05)
        # Full-page scroll: try pagedown (Windows), then Fn+Down (Mac)
        if direction == 'down':
            pyautogui.press('pagedown')
            time.sleep(0.05)
            pyautogui.hotkey('fn', 'down')
        elif direction == 'up':
            pyautogui.press('pageup')
            time.sleep(0.05)
            pyautogui.hotkey('fn', 'up')
        logger.info(f"  → scroll {direction} (pagedown + fn+arrow)")
        time.sleep(0.3)

    elif action_type == 'mouse_move':
        if coordinate:
            abs_x = int(coordinate[0] * scale_x) + region['x']
            abs_y = int(coordinate[1] * scale_y) + region['y']
            pyautogui.moveTo(abs_x, abs_y)

    elif action_type == 'wait':
        duration = action.get('duration', 1)
        time.sleep(min(duration, 5))  # Cap at 5s
        logger.info(f"  → wait {duration}s")

    else:
        logger.warning(f"  → Unknown action type: {action_type}")


def get_tb_coords(server_url: str, screenshot_bytes: bytes,
                          json_data: dict, already_filled: set, job_id: int = None) -> dict:
    """
    Send screenshot + job_id to the server, which loads the quote page images
    and calls Claude via Bedrock to match source data to form fields.
    Falls back to json_data if job_id is not available.
    """
    skip_list = sorted(already_filled) if already_filled else []

    payload = {
        'screenshot': base64.b64encode(screenshot_bytes).decode('ascii'),
        'json_data': json_data,
        'already_filled': skip_list,
    }
    if job_id:
        payload['job_id'] = job_id

    try:
        response = requests.post(
            f"{server_url}/api/ams/vision",
            json=payload,
            timeout=120
        )
        response.raise_for_status()
        data = response.json()

        if not data.get('success'):
            raise ValueError(data.get('error', 'Unknown server error'))

        field_map = data.get('field_map', {})
        logger.info(f"Server returned {len(field_map)} field matches")
        return field_map

    except requests.exceptions.RequestException as e:
        logger.error(f"Vision API request failed: {e}")
        return {}
    except Exception as e:
        logger.error(f"Vision API error: {e}")
        return {}

def verify_field_filled(value: str) -> str:
    """
    After pasting into a field, select-all + copy to read back what's in the
    focused element. Returns a verdict: 'hit', 'miss_empty', 'miss_page_selected'.

    Heuristic:
      - Clipboard contains the pasted value (substring) → hit
      - Clipboard is empty or unchanged → miss (click landed on non-editable)
      - Clipboard is >5x longer than value → miss (Cmd+A selected the whole page)
    """
    time.sleep(VERIFY_DELAY)
    # Clear clipboard so we can detect "nothing copied"
    pyperclip.copy("")
    pyautogui.hotkey(*SELECT_HOTKEY)
    time.sleep(0.04)
    pyautogui.hotkey(*COPY_HOTKEY)
    time.sleep(0.04)

    clipboard = pyperclip.paste().strip()

    if not clipboard:
        return "miss_empty"
    # If clipboard is way longer than our value, we probably selected the whole page
    if len(clipboard) > max(len(value) * 5, 200):
        return "miss_page_selected"
    # Check if our value is present in what was selected
    if value.lower() in clipboard.lower():
        return "hit"
    # The field has *something* but not our value — could be a pre-filled field
    # we accidentally clicked, or the paste didn't take
    return "miss_wrong_content"


def capture_miss_thumbnail(abs_x: int, abs_y: int, label: str, attempt: int, region: dict):
    """
    Capture a small screenshot (~200x120px) around the click point for debugging.
    Saved to the debug directory with field name and attempt number.
    """
    # Wide thumbnail so the field's label (usually to the left) is visible
    thumb_w, thumb_h = 420, 100
    # Center the thumbnail on the click point, but clamp to screen
    tx = abs_x - thumb_w // 2
    ty = abs_y - thumb_h // 2

    try:
        if USE_MSS:
            with mss.mss() as sct:
                monitor = {"left": tx, "top": ty, "width": thumb_w, "height": thumb_h}
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        else:
            bbox = (tx, ty, tx + thumb_w, ty + thumb_h)
            img = ImageGrab.grab(bbox=bbox, all_screens=True)

        # Draw a red crosshair at the click point (center of thumbnail)
        draw = ImageDraw.Draw(img)
        cx, cy = thumb_w // 2, thumb_h // 2
        draw.line([(cx - 10, cy), (cx + 10, cy)], fill="red", width=2)
        draw.line([(cx, cy - 10), (cx, cy + 10)], fill="red", width=2)

        safe_label = re.sub(r'[^\w\-]', '_', label)[:40]
        timestamp = time.strftime("%H%M%S")
        filename = f"miss_{safe_label}_attempt{attempt}_{timestamp}.png"
        path = DEBUG_FILL_DIR / filename
        img.save(str(path))
        logger.info(f"  📸 Miss thumbnail saved: {path}")
    except Exception as e:
        logger.warning(f"  Could not save miss thumbnail: {e}")


def save_annotated_screenshot(region: dict, click_log: list, job_id: int = None):
    """
    Take a full screenshot of the form region and draw X markers at every
    click coordinate. Each X is labeled with the field name and color-coded:
      green = verified hit, red = miss after all retries, yellow = unverified.
    """
    try:
        if USE_MSS:
            with mss.mss() as sct:
                monitor = {
                    "left": region["x"], "top": region["y"],
                    "width": region["width"], "height": region["height"],
                }
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        else:
            bbox = (region["x"], region["y"],
                    region["x"] + region["width"], region["y"] + region["height"])
            img = ImageGrab.grab(bbox=bbox, all_screens=True)

        draw = ImageDraw.Draw(img)

        for entry in click_log:
            # Convert absolute coords to relative within the region
            rx = entry["abs_x"] - region["x"]
            ry = entry["abs_y"] - region["y"]
            status = entry.get("status", "unknown")

            color = {"hit": "#00cc44", "miss": "#ff2222", "unknown": "#ffaa00"}.get(status, "#ffaa00")

            # Draw X marker
            size = 12
            draw.line([(rx - size, ry - size), (rx + size, ry + size)], fill=color, width=2)
            draw.line([(rx - size, ry + size), (rx + size, ry - size)], fill=color, width=2)

            # Label
            label_text = f"{entry['label']}"
            draw.text((rx + size + 3, ry - 8), label_text, fill=color)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        job_tag = f"_job{job_id}" if job_id else ""
        filename = f"fill_map{job_tag}_{timestamp}.png"
        path = DEBUG_FILL_DIR / filename
        img.save(str(path))
        logger.info(f"📋 Annotated fill map saved: {path}")
        print(f"  Fill map: {path}")
    except Exception as e:
        logger.warning(f"Could not save annotated screenshot: {e}")


def tb_fill(tb_dict: dict, region: dict, scale: float, job_id: int = None) -> set:
    filled = set()
    failed = set()
    click_log = []  # Track all click attempts for the annotated screenshot

    # Click somewhere safe first to ensure browser address bar isn't focused
    safe_x = region["x"] + region["width"] // 2
    safe_y = region["y"] + region["height"] // 2
    pyautogui.click(safe_x, safe_y)
    time.sleep(0.05)

    for path, info in flatten_with_path(tb_dict):
        label = path.split(".")[-1]
        # Skip metadata keys
        if label.startswith("__") or not isinstance(info, dict):
            continue
        value = str(info.get("value", "")).strip()
        if not value:
            logger.debug(f"Skipping '{label}' — no value")
            continue
        if info.get("field_type") != "text_field":
            logger.debug(f"Skipping '{label}' — not a text field")
            continue

        base_x = int(info["x"] / scale) + region["x"]
        base_y = int(info["y"] / scale) + region["y"]

        field_filled = False

        # ── ATTEMPT 0: Accessibility API (macOS) ─────────────────────────────
        # Snap to the nearest text field and set its value directly.
        if _AX_AVAILABLE:
            ax_ok, ax_info = ax_fill_field(base_x, base_y, value)
            if ax_ok:
                logger.info(
                    f"✓ FILLED '{label}' | via AX | {ax_info} | "
                    f"value='{value}' | coords=({base_x},{base_y})"
                )
                click_log.append({
                    "label": label, "abs_x": base_x, "abs_y": base_y,
                    "status": "hit", "attempt": 0, "value": value, "method": "ax",
                })
                filled.add(label)
                field_filled = True
            else:
                logger.info(
                    f"  AX did not succeed for '{label}' ({ax_info}) — falling back to click+paste"
                )

        # ── FALLBACK: click + Cmd+A + paste with retries ─────────────────────
        # Retry offsets: try center, then below (labels usually above field),
        # then above, then sideways. Larger offsets to actually land on the box.
        retry_offsets = [(0, 0), (0, 18), (0, -18), (15, 0), (-15, 0)]

        for attempt_idx, (dx, dy) in enumerate(retry_offsets):
            if field_filled:
                break
            attempt = attempt_idx + 1
            if attempt > MAX_FIELD_RETRIES:
                break
            abs_x = base_x + dx
            abs_y = base_y + dy

            try:
                pyautogui.click(abs_x, abs_y)
                time.sleep(CLICK_DELAY)
                pyautogui.hotkey(*SELECT_HOTKEY)

                if REMOTE_MODE:
                    # Remote mode: typewrite keystrokes (no clipboard access)
                    pyautogui.typewrite(value, interval=TYPEWRITE_INTERVAL)
                else:
                    pyperclip.copy(value)
                    pyautogui.hotkey(*PASTE_HOTKEY)
                time.sleep(FILL_DELAY)

                if REMOTE_MODE:
                    # Skip verification in remote mode — clipboard isn't shared
                    logger.info(
                        f"✓ FILLED '{label}' (remote, unverified) | attempt {attempt} (offset {dx},{dy}) | "
                        f"value='{value}' | coords=({abs_x},{abs_y})"
                    )
                    click_log.append({
                        "label": label, "abs_x": abs_x, "abs_y": abs_y,
                        "status": "hit", "attempt": attempt, "value": value, "method": "typewrite",
                    })
                    filled.add(label)
                    field_filled = True
                    pyautogui.click(safe_x, safe_y)
                    time.sleep(0.03)
                    break

                # Verify the paste landed
                verdict = verify_field_filled(value)

                if verdict == "hit":
                    logger.info(
                        f"✓ FILLED '{label}' | attempt {attempt} (offset {dx},{dy}) | "
                        f"value='{value}' | coords=({abs_x},{abs_y})"
                    )
                    click_log.append({
                        "label": label, "abs_x": abs_x, "abs_y": abs_y,
                        "status": "hit", "attempt": attempt, "value": value, "method": "click",
                    })
                    filled.add(label)
                    field_filled = True
                    # Click safe spot to deselect before moving to next field
                    pyautogui.click(safe_x, safe_y)
                    time.sleep(0.03)
                    break
                else:
                    logger.warning(
                        f"✗ MISS '{label}' | attempt {attempt}/{MAX_FIELD_RETRIES} (offset {dx},{dy}) | "
                        f"verdict={verdict} | value='{value}' | coords=({abs_x},{abs_y})"
                    )
                    # Capture a thumbnail of the miss area
                    capture_miss_thumbnail(abs_x, abs_y, label, attempt, region)
                    # Press Escape to deselect / dismiss anything before retry
                    pyautogui.press("escape")
                    time.sleep(0.05)
                    # Click safe spot to reset focus
                    pyautogui.click(safe_x, safe_y)
                    time.sleep(0.05)

            except Exception as e:
                logger.error(
                    f"✗ EXCEPTION '{label}' | attempt {attempt}/{MAX_FIELD_RETRIES} | "
                    f"coords=({abs_x},{abs_y}) | error: {e}"
                )
                pyautogui.click(safe_x, safe_y)
                time.sleep(0.05)

        if not field_filled:
            logger.error(
                f"✗✗ FAILED '{label}' after all attempts | "
                f"value='{value}' | base_coords=({base_x},{base_y})"
            )
            click_log.append({
                "label": label, "abs_x": base_x, "abs_y": base_y,
                "status": "miss", "attempt": MAX_FIELD_RETRIES, "value": value,
            })
            failed.add(label)

    # Save annotated full-page screenshot with all click locations
    save_annotated_screenshot(region, click_log, job_id=job_id)

    # Summary log
    logger.info(
        f"Fill summary: {len(filled)} filled, {len(failed)} failed "
        f"| filled={sorted(filled)} | failed={sorted(failed)}"
    )
    if failed:
        print(f"  ⚠️  Failed fields ({len(failed)}): {sorted(failed)}")
        print(f"  Debug images: {DEBUG_FILL_DIR}")

    return filled

class SelectionPopup:
    """
    Draggable popup shown before work starts.
    User drags it onto the AMS window and clicks 'Push Data Here'.
    Destroyed immediately after the click so it's never in screenshots.
    """
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("AMS Agent")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg="#0f1219")
        self.root.resizable(False, False)

        w = 260
        h = 195
        screen_w = self.root.winfo_screenwidth()
        self.root.geometry(f"{w}x{h}+{screen_w - w - 20}+80")

        # State
        self.position_result = None
        self.should_close = False
        self.drag = {"x": 0, "y": 0}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        logger.info("Selection popup created")

    def _build_ui(self):
        # Header / drag handle
        hdr = self.tk.Frame(self.root, bg="#0f1219", cursor="fleur")
        hdr.pack(fill="x")
        hdr.bind("<ButtonPress-1>", self._drag_start)
        hdr.bind("<B1-Motion>", self._drag_move)

        inner = self.tk.Frame(hdr, bg="#0f1219")
        inner.pack(fill="x", padx=14, pady=(10, 4))
        inner.bind("<ButtonPress-1>", self._drag_start)
        inner.bind("<B1-Motion>", self._drag_move)

        title = self.tk.Label(inner, text="RiskRunway", font=("Segoe UI", 10, "bold"),
                              fg="#c8cfe0", bg="#0f1219")
        title.pack(side="left")
        title.bind("<ButtonPress-1>", self._drag_start)
        title.bind("<B1-Motion>", self._drag_move)

        subtitle = self.tk.Label(inner, text="export agent",
                                 font=("Segoe UI", 8), fg="#4a5270", bg="#0f1219")
        subtitle.pack(side="left", padx=(6, 0))
        subtitle.bind("<ButtonPress-1>", self._drag_start)
        subtitle.bind("<B1-Motion>", self._drag_move)

        # Separator
        self.tk.Frame(self.root, bg="#1e2538", height=1).pack(fill="x", padx=14)

        # Body
        body = self.tk.Frame(self.root, bg="#0f1219")
        body.pack(fill="both", expand=True, padx=14, pady=(10, 10))

        # Instruction
        self.tk.Label(
            body, text="Drag onto target window",
            font=("Segoe UI", 8), fg="#6b7394", bg="#0f1219",
        ).pack(anchor="w", pady=(0, 6))

        # Main button
        self.tk.Button(
            body, text="Push Data Here",
            font=("Segoe UI", 9, "bold"), fg="#0f1219", bg="#c8cfe0",
            activebackground="#a8b4cc", activeforeground="#0f1219",
            relief="flat", cursor="hand2", pady=6,
            command=self._on_push,
        ).pack(fill="x")

        # Bottom row: 2 columns — disclaimer left, buttons right
        bottom = self.tk.Frame(body, bg="#0f1219")
        bottom.pack(fill="x", pady=(6, 0))

        # Left column: disclaimer
        self.tk.Label(
            bottom, text="Verify all values\nbefore saving.",
            font=("Segoe UI", 7), fg="#6b7394", bg="#0f1219",
            justify="center",
        ).pack(side="left", anchor="s")

        # Right column: remote push + close stacked
        right_col = self.tk.Frame(bottom, bg="#0f1219")
        right_col.pack(side="right", anchor="se")

        remote_link = self.tk.Label(
            right_col, text="remote push",
            font=("Segoe UI", 8), fg="#4f8ef7", bg="#0f1219",
            cursor="hand2",
        )
        remote_link.pack(anchor="e")
        remote_link.bind("<Button-1>", lambda e: self._on_remote_push())
        remote_link.bind("<Enter>", lambda e: remote_link.config(fg="#7aabff"))
        remote_link.bind("<Leave>", lambda e: remote_link.config(fg="#4f8ef7"))

        close_link = self.tk.Label(
            right_col, text="close",
            font=("Segoe UI", 8), fg="#c0392b", bg="#0f1219",
            cursor="hand2",
        )
        close_link.pack(anchor="e", pady=(2, 0))
        close_link.bind("<Button-1>", lambda e: self._on_close())
        close_link.bind("<Enter>", lambda e: close_link.config(fg="#e74c3c"))
        close_link.bind("<Leave>", lambda e: close_link.config(fg="#c0392b"))

        self.root.configure(highlightbackground="#1e2538", highlightthickness=1)

    def _drag_start(self, e):
        self.drag["x"] = e.x_root - self.root.winfo_x()
        self.drag["y"] = e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self.drag['x']}+{e.y_root - self.drag['y']}")

    def _on_push(self):
        cx = self.root.winfo_x() + self.root.winfo_width() // 2
        cy = self.root.winfo_y() + self.root.winfo_height() // 2
        self.position_result = (cx, cy)
        logger.info(f"User clicked Push Data Here at ({cx}, {cy})")

    def _on_remote_push(self):
        global REMOTE_MODE
        REMOTE_MODE = True
        cx = self.root.winfo_x() + self.root.winfo_width() // 2
        cy = self.root.winfo_y() + self.root.winfo_height() // 2
        self.position_result = (cx, cy)
        logger.info(f"User clicked Remote Push at ({cx}, {cy}) — remote mode enabled")

    def _on_close(self):
        self.should_close = True
        logger.info("User cancelled selection popup")

    def update(self):
        try:
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            pass

    def wait_for_click(self) -> Optional[tuple]:
        """Block until user clicks or closes. Returns (x, y) or None."""
        self.position_result = None
        logger.info("Waiting for user to click 'Push Data Here'...")

        while self.position_result is None and not self.should_close:
            self.update()
            time.sleep(0.01)

        if self.should_close:
            return None
        return self.position_result

    def destroy(self):
        try:
            self.root.destroy()
        except:
            pass
        logger.info("Selection popup destroyed")


class SpinnerOverlay:
    """
    A tiny, borderless floating spinner shown during export work.
    No window chrome — just an animated character on a small pill.
    Positioned where the selection popup was (where user clicked).
    """
    def __init__(self, x: int = 16, y: int = 16):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.85)
        self.root.configure(bg="#1a1f2e")
        self.root.resizable(False, False)

        # Position where the popup was
        self.root.geometry(f"140x36+{x}+{y}")

        self.spinner_chars = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
        self.spinner_idx = 0
        self._last_tick = 0.0

        frame = tk.Frame(self.root, bg="#1a1f2e")
        frame.pack(fill="both", expand=True, padx=6, pady=4)

        self.spinner_label = tk.Label(
            frame, text=self.spinner_chars[0],
            font=("Courier", 14), fg="#4f8ef7", bg="#1a1f2e",
        )
        self.spinner_label.pack(side="left", padx=(2, 4))

        self.status_label = tk.Label(
            frame, text="exporting...",
            font=("Helvetica", 10), fg="#8892b0", bg="#1a1f2e",
        )
        self.status_label.pack(side="left")

        self.root.configure(highlightbackground="#4f8ef7", highlightthickness=1)
        logger.info("Spinner overlay created")

    def set_text(self, text: str):
        try:
            self.status_label.config(text=text)
        except self.tk.TclError:
            pass

    def update(self):
        """Call regularly to animate the spinner and process events."""
        try:
            now = time.time()
            if now - self._last_tick > 0.08:
                self.spinner_idx = (self.spinner_idx + 1) % len(self.spinner_chars)
                self.spinner_label.config(text=self.spinner_chars[self.spinner_idx])
                self._last_tick = now
            # Keep on top even when pyautogui clicks steal focus
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            pass

    def flash_result(self, success: bool):
        """No-op — spinner just stays until destroy() is called."""
        pass

    def destroy(self):
        try:
            self.root.destroy()
        except:
            pass
        logger.info("Spinner overlay destroyed")


class PersistentOverlay:
    """
    Wraps SelectionPopup + SpinnerOverlay into the interface expected by run_job.
    Phase 1: Shows SelectionPopup for window selection.
    Phase 2: Destroys popup, shows SpinnerOverlay during work.
    Phase 3: Flashes result, destroys spinner.
    """
    def __init__(self):
        self.popup = SelectionPopup()
        self.spinner = None
        self.should_close = False

    def wait_for_click(self) -> Optional[tuple]:
        result = self.popup.wait_for_click()
        if self.popup.should_close:
            self.should_close = True
        return result

    def begin_work(self):
        """Destroy the popup and show the spinner where the popup was."""
        # Grab popup position before destroying it
        try:
            px = self.popup.root.winfo_x()
            py = self.popup.root.winfo_y()
        except:
            px, py = 16, 16
        self.popup.destroy()
        self.spinner = SpinnerOverlay(x=px, y=py)

    def set_status(self, status: str, color: str = "#5a6180", indicator_color: str = "#2ecc8a"):
        if self.spinner:
            self.spinner.set_text(status)

    def set_button_enabled(self, enabled: bool):
        pass  # No button in spinner mode

    def update(self):
        if self.spinner:
            self.spinner.update()
        elif self.popup:
            self.popup.update()

    def flash_result(self, success: bool):
        pass  # Spinner stays visible until destroy() is called

    def destroy(self):
        if self.spinner:
            self.spinner.destroy()
            self.spinner = None
        if self.popup:
            try:
                self.popup.destroy()
            except:
                pass
        logger.info("Overlay destroyed")

def prompt_user_to_select_window() -> Optional[dict]:
    """Use the persistent overlay to get window selection."""
    global persistent_overlay

    if persistent_overlay is None:
        logger.error("Persistent overlay not initialized!")
        return None

    # Wait for user to click
    pos = persistent_overlay.wait_for_click()
    if pos is None:
        logger.info("User cancelled")
        return None

    x, y = pos
    time.sleep(0.5)
    window_region = _get_window_region_at(x, y)
    region = inset_region(
        window_region,
        top=FORM_REGION_INSET_TOP,
        left=FORM_REGION_INSET_LEFT,
        right=FORM_REGION_INSET_RIGHT,
        bottom=FORM_REGION_INSET_BOTTOM,
    )
    logger.info(
        "Form viewport region: "
        f"{region['width']}x{region['height']} at ({region['x']},{region['y']}) "
        f"from window {window_region['width']}x{window_region['height']} "
        f"at ({window_region['x']},{window_region['y']})"
    )
    print("\n  Window selected — Claude is starting now!\n")
    return region

# def _get_window_region_at(x: int, y: int) -> dict:
#     """Return bounding box of the window at screen position (x, y)."""

#     # macOS via Quartz
#     try:
#         from Quartz import (CGWindowListCopyWindowInfo,
#                             kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
#         windows = CGWindowListCopyWindowInfo(
#             kCGWindowListOptionOnScreenOnly, kCGNullWindowID
#         )
#         for win in windows:
#             b  = win.get("kCGWindowBounds", {})
#             wx, wy = int(b.get("X", 0)), int(b.get("Y", 0))
#             ww, wh = int(b.get("Width", 0)), int(b.get("Height", 0))
#             if wx <= x <= wx + ww and wy <= y <= wy + wh and ww > 50 and wh > 50:
#                 title  = win.get("kCGWindowName") or win.get("kCGWindowOwnerName", "")
#                 region = {"x": wx, "y": wy, "width": ww, "height": wh}
#                 logger.info(f"Window (macOS): '{title}' {ww}x{wh} at ({wx},{wy})")
#                 return region
#     except ImportError:
#         pass

#     # Windows via pywin32
#     try:
#         import win32gui
#         hwnd = win32gui.WindowFromPoint((x, y))
#         if hwnd:
#             wx, wy, wx2, wy2 = win32gui.GetWindowRect(hwnd)
#             title  = win32gui.GetWindowText(hwnd)
#             region = {"x": wx, "y": wy, "width": wx2 - wx, "height": wy2 - wy}
#             logger.info(f"Window (Windows): '{title}' at {region}")
#             return region
#     except ImportError:
#         pass

#     # Fallback: full screen
#     logger.warning("Could not detect window bounds — using full screen")
#     w, h = pyautogui.size()
#     return {"x": 0, "y": 0, "width": w, "height": h}

def _get_window_region_at(x: int, y: int) -> dict:
    # macOS via Quartz
    try:
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        import os
        current_pid = os.getpid()
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        for win in windows:
            if win.get("kCGWindowOwnerPID") == current_pid:
                continue  # skip anything owned by this Python process
            b  = win.get("kCGWindowBounds", {})
            wx, wy = int(b.get("X", 0)), int(b.get("Y", 0))
            ww, wh = int(b.get("Width", 0)), int(b.get("Height", 0))
            if wx <= x <= wx + ww and wy <= y <= wy + wh and ww > 50 and wh > 50:
                title  = win.get("kCGWindowName") or win.get("kCGWindowOwnerName", "")
                region = {"x": wx, "y": wy, "width": ww, "height": wh}
                logger.info(f"Window (macOS): '{title}' {ww}x{wh} at ({wx},{wy})")
                return region
    except ImportError:
        pass

    # Windows via pywin32
    try:
        import win32gui
        import win32process
        import os
        current_pid = os.getpid()
        hwnd = win32gui.WindowFromPoint((x, y))
        if hwnd:
            # Walk up to the top-level window (not a child control)
            ancestor = win32gui.GetAncestor(hwnd, 2)  # GA_ROOT = 2
            if ancestor:
                hwnd = ancestor
            # If we found our own overlay, look for the window behind it
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == current_pid:
                logger.debug("WindowFromPoint hit our own overlay, looking behind it")
                # Temporarily hide our overlay to find what's underneath
                overlay_hwnd = hwnd
                win32gui.ShowWindow(overlay_hwnd, 0)  # SW_HIDE
                time.sleep(0.05)
                hwnd = win32gui.WindowFromPoint((x, y))
                if hwnd:
                    ancestor = win32gui.GetAncestor(hwnd, 2)
                    if ancestor:
                        hwnd = ancestor
                win32gui.ShowWindow(overlay_hwnd, 5)  # SW_SHOW
            wx, wy, wx2, wy2 = win32gui.GetWindowRect(hwnd)
            ww, wh = wx2 - wx, wy2 - wy
            if ww > 50 and wh > 50:
                title = win32gui.GetWindowText(hwnd)
                region = {"x": wx, "y": wy, "width": ww, "height": wh}
                logger.info(f"Window (Windows): '{title}' {ww}x{wh} at ({wx},{wy})")
                return region
    except ImportError:
        pass

    # Fallback: full screen
    logger.warning("Could not detect window bounds — using full screen")
    w, h = pyautogui.size()
    return {"x": 0, "y": 0, "width": w, "height": h}

def poll_for_job(server_url: str) -> Optional[dict]:
    try:
        r = requests.get(f"{server_url}/api/ams/jobs/next", timeout=5)
        if r.status_code == 200:
            return r.json().get("job")
    except requests.exceptions.ConnectionError:
        logger.warning(f"Cannot reach {server_url} — retrying...")
    except Exception as e:
        logger.warning(f"Poll error: {e}")
    return None

def fetch_job_by_id(server_url: str, job_id: int) -> Optional[dict]:
    """Fetch a specific job by ID for single-shot mode."""
    try:
        r = requests.get(f"{server_url}/api/ams/jobs/{job_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("job"):
                return data.get("job")
            else:
                logger.error(f"Job {job_id} not found or not available: {data.get('error', 'Unknown')}")
        else:
            logger.error(f"Failed to fetch job {job_id}: HTTP {r.status_code}")
    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot reach {server_url}")
    except Exception as e:
        logger.error(f"Fetch job error: {e}")
    return None

def update_job_status(server_url: str, job_id: int, status: str, message: str = ""):
    payload = {"status": status}
    if message:
        payload["message"] = message
    try:
        requests.patch(
            f"{server_url}/api/ams/jobs/{job_id}/status",
            json=payload, timeout=5,
        )
        logger.info(f"Job {job_id} -> {status}")
    except Exception as e:
        logger.error(f"Status update failed for job {job_id}: {e}")

def run_job(job: dict, server_url: str):
    global persistent_overlay, REMOTE_MODE

    # Reset remote mode for each job — user picks per-job via button
    REMOTE_MODE = False

    job_id    = job["id"]
    json_data = job.get("json_data") or {}

    # json_data may arrive as a JSON string — parse it if so
    if isinstance(json_data, str):
        try:
            json_data = json.loads(json_data)
        except Exception:
            logger.error("json_data was a string but could not be parsed as JSON")
            json_data = {}

    print(f"\n{'='*52}")
    print(f"  New AMS Export Job #{job_id}")
    print(f"{'='*52}\n")
    logger.info(f"Job data keys: {list(json_data.keys()) if isinstance(json_data, dict) else type(json_data)}")

    # Get window region from user
    region = prompt_user_to_select_window()
    if region is None:
        update_job_status(server_url, job_id, "failed", "User cancelled")
        persistent_overlay.destroy()
        return

    logger.info(f"Target region: {region}")

    # Destroy the selection popup and show the tiny spinner
    persistent_overlay.begin_work()

    # Store result from thread to avoid widget updates in background thread
    result = {"success": None, "error": None}

    # Define the work function to run in a thread
    def do_work():
        try:
            success = run_computer_use_job(server_url, region, job_id=job_id)
            result["success"] = success
            if success:
                update_job_status(server_url, job_id, "complete")
                print(f"\n  Job #{job_id} complete!")
            else:
                update_job_status(server_url, job_id, "failed", "No fields were filled")
                print(f"\n  Job #{job_id} — no fields could be filled")
        except Exception as e:
            logger.error(f"Job {job_id} error: {e}", exc_info=True)
            update_job_status(server_url, job_id, "failed", str(e))
            print(f"\n  Job #{job_id} error: {e}")
            result["error"] = str(e)

    # Run the heavy work in a background thread to keep UI responsive
    work_thread = threading.Thread(target=do_work, daemon=True)
    work_thread.start()

    # Keep updating the widget while work is happening
    while work_thread.is_alive():
        persistent_overlay.update()
        time.sleep(0.05)

    # Flash result and auto-destroy
    if result.get("error"):
        persistent_overlay.flash_result(False)
    elif result.get("success"):
        persistent_overlay.flash_result(True)
    else:
        persistent_overlay.flash_result(False)

    persistent_overlay.destroy()

def polling_loop(server_url: str):
    """Runs in background. Puts jobs on job_queue for the main thread."""
    while True:
        try:
            job = poll_for_job(server_url)
            if job:
                job_queue.put(job)
                job_queue.join()   # wait for main thread to finish before polling again
            else:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(POLL_INTERVAL)

def main():
    global persistent_overlay

    parser = argparse.ArgumentParser(description="AMS Export Agent — vision-map")
    parser.add_argument("--server",     default=DEFAULT_SERVER_URL,
                        help=f"Flask server URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--job-id", type=int, default=None,
                        help="Run in single-shot mode: fetch specific job ID, execute, then exit")
    parser.add_argument("--daemon", action="store_true",
                        help="Run in daemon mode: continuously poll for jobs (default behavior)")
    parser.add_argument("url", nargs="?", default=None,
                        help="Optional riskrunway:// protocol URL (parsed for job_id and server)")
    args       = parser.parse_args()

    # If a riskrunway:// URL was passed (from protocol handler), parse it
    if args.url and args.url.startswith("riskrunway://"):
        import urllib.parse
        parsed = urllib.parse.urlparse(args.url)
        params = urllib.parse.parse_qs(parsed.query)
        if 'job_id' in params and args.job_id is None:
            args.job_id = int(params['job_id'][0])
        if 'server' in params and args.server == DEFAULT_SERVER_URL:
            args.server = urllib.parse.unquote(params['server'][0])

    server_url = args.server.rstrip("/")

    # Determine mode: single-shot if job_id provided, otherwise daemon (or explicit --daemon)
    is_single_shot = args.job_id is not None
    
    print(f"""
  AMS Export Agent
  ─────────────────────────────────
  Agent ID : {AGENT_ID}
  Server   : {server_url}
  Mode     : {'Single-shot (Job #' + str(args.job_id) + ')' if is_single_shot else 'Daemon (continuous polling)'}
  OS       : {platform.system()}
  Input    : Choose in popup — 'Push Data Here' (paste) or 'Remote Push' (typewrite)
    """)

    if is_single_shot:
        # SINGLE-SHOT MODE: Fetch specific job, execute, exit
        logger.info(f"Single-shot mode: fetching job {args.job_id}")
        job = fetch_job_by_id(server_url, args.job_id)
        
        if job is None:
            print(f"\n❌ Could not fetch job {args.job_id}. Exiting.")
            sys.exit(1)
        
        # Create overlay just for this job
        persistent_overlay = PersistentOverlay()
        logger.info("Overlay created for single-shot job")
        
        # Execute the job
        try:
            run_job(job, server_url)
        except Exception as e:
            logger.error(f"Job execution failed: {e}", exc_info=True)
            print(f"\n❌ Job {args.job_id} failed: {e}")
        
        # Clean up and exit
        if persistent_overlay:
            persistent_overlay.destroy()
        print(f"\n✓ Job {args.job_id} complete. Exiting.")
        _close_console_window()
        sys.exit(0)
    
    else:
        # DAEMON MODE: Continuous polling (original behavior)
        # Create the persistent overlay widget (must be on main thread for macOS)
        persistent_overlay = PersistentOverlay()
        logger.info("Persistent overlay widget created and ready")

        # Polling runs in background; tkinter must stay on main thread
        threading.Thread(
            target=polling_loop, args=(server_url,), daemon=True
        ).start()
        logger.info(f"Polling {server_url} every {POLL_INTERVAL}s...")
        print("Waiting for jobs — Ctrl+C to stop.\n")

        while True:
            try:
                # Keep the overlay widget responsive
                if persistent_overlay and not persistent_overlay.should_close:
                    persistent_overlay.update()

                # Check if user closed the widget
                if persistent_overlay and persistent_overlay.should_close:
                    print("\nWidget closed by user. Shutting down...")
                    break

                # Check for jobs (non-blocking with short timeout)
                try:
                    job = job_queue.get(timeout=0.01)
                    run_job(job, server_url)
                    job_queue.task_done()
                except queue.Empty:
                    pass

                # Small sleep to prevent CPU spinning
                time.sleep(0.01)

            except KeyboardInterrupt:
                print("\nAgent stopped.")
                break

        # Clean up
        if persistent_overlay:
            persistent_overlay.destroy()
        _close_console_window()
        sys.exit(0)

if __name__ == "__main__":
    main()
