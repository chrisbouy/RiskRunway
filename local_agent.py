#!/usr/bin/env python3
"""
AMS Export Agent — Vision-map, Claude does all field matching.

How it works:
  1. Take ONE screenshot of the AMS window.
  2. Send the screenshot AND the job's JSON data to Claude together.
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
import re
import sys
import tempfile
import threading
import time
import uuid
import argparse
from pathlib import Path
from typing import Optional

import boto3
from msal import region
import pyautogui
import pyperclip
import requests

try:
    import mss
    from PIL import Image, ImageDraw
    USE_MSS = True
except ImportError:
    from PIL import ImageGrab, Image
    USE_MSS = False

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SERVER_URL = "http://localhost:5001"
AGENT_ID           = str(uuid.uuid4())[:8]
POLL_INTERVAL      = 0.5   # seconds between idle polls
MAX_SCROLL_PASSES  = 5     # max scroll passes per job (safety limit)
CLICK_DELAY        = 0.08  # seconds to wait after clicking a field
FILL_DELAY         = 0.06  # seconds between filling each field

AWS_REGION = "us-east-1"
MODEL_ID   = "us.anthropic.claude-sonnet-4-20250514-v1:0"

IS_MAC        = platform.system() == "Darwin"
PASTE_HOTKEY  = ("command", "v") if IS_MAC else ("ctrl", "v")
SELECT_HOTKEY = ("command", "a") if IS_MAC else ("ctrl", "a")

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

# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing — handles whatever format Claude returns
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# Screenshot
# ─────────────────────────────────────────────────────────────────────────────

def take_screenshot(region: dict,marker: tuple = None) -> bytes:
    """
    Capture the given screen region and return raw PNG bytes.
    mss handles negative y coordinates correctly on macOS dual-monitor setups.
    Also saves a debug copy to /tmp/ams_debug_last.png for inspection.
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
    
    timestamp = time.strftime("%Y%m%d_%H%M%S_%f")
    debug_path = Path(tempfile.gettempdir()) / f"ams_debug_{timestamp}.png"
    img.save(str(debug_path))
    logger.info(f"Screenshot: {debug_path} ({img.width}x{img.height})")

    buf = io.BytesIO()
    # if marker:
    #     draw = ImageDraw.Draw(img)
    #     mx, my = marker
    #     # Red crosshair, 20px size
    #     print(f"Drawing marker at ({mx},{my})")
    #     draw.line([(mx-20, my), (mx+20, my)], fill="red", width=2)
    #     draw.line([(mx, my-20), (mx, my+20)], fill="red", width=2)
    img.save(buf, format="PNG")
    
    
    return buf.getvalue()

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

# ─────────────────────────────────────────────────────────────────────────────
# Vision job loop — screenshot → fill → scroll → repeat
# ─────────────────────────────────────────────────────────────────────────────

def run_vision_job(bedrock_client, json_data: dict, region: dict) -> bool:
    all_filled: set = set()
    remaining_data  = flatten_job_data(json_data)   # starts full, shrinks each pass

    for pass_num in range(MAX_SCROLL_PASSES):
        logger.info(f"--- Pass {pass_num + 1}/{MAX_SCROLL_PASSES} ---")
        logger.info(f"Remaining data keys: {list(remaining_data.keys())}")
        if not remaining_data:
            logger.info("All data placed — done")
            break
        
        #textbox pass
        # time.sleep(2)
        print(f"\n  Taking screenshot for claude's tb pass {pass_num + 1}...")
        screenshot   = take_screenshot(region)
        textboxes_json = get_tb_coords(bedrock_client, screenshot, remaining_data, all_filled)
        field_count = len([k for k in textboxes_json if not k.startswith("__")])
        newly_filled = tb_fill(textboxes_json, region)
        all_filled.update(newly_filled)
        
        #dropdown pass
        time.sleep(2)
        print(f"\n  Taking screenshot for claude's dropdown pass {pass_num + 1}...")
        screenshot   = take_screenshot(region)
        dropdowns_json = get_dropdown_coords(bedrock_client, screenshot,remaining_data)
        newly_filled = ddl_fill(bedrock_client,dropdowns_json,remaining_data, region)
        all_filled.update(newly_filled)
        
        field_count = field_count + len([k for k in dropdowns_json if not k.startswith("__")])
        if field_count == 0:
            logger.info("No fillable fields returned — done")
            break
        
        

        # Remove the data values that got placed this pass
        for label, info in get_tb_coords.items():
            if label.startswith("__") or label not in newly_filled:
                continue
            value_placed = str(info.get("value", "")).strip()
            # Find and remove the matching key from remaining_data
            for data_key, data_val in list(remaining_data.items()):
                if str(data_val).strip() == value_placed:
                    logger.info(f"  Removing '{data_key}' from remaining data")
                    del remaining_data[data_key]
                    break

        if not newly_filled:
            logger.info("No new fields filled — done")
            break

        # Scroll for next pass
        last_filled_form_field = list(newly_filled)[-1]  #returns a random element from the set, we just want any one of the filled fields to scroll from
        last_info  = get_tb_coords.get(last_filled_form_field, {})
        last_x     = int(last_info.get("x", 0)) + region["x"]
        last_y     = int(last_info.get("y", 0)) + region["y"]
        pyautogui.click(last_x, last_y-10)
        # time.sleep(1)
        print(f"  Scrolling down for next pass...")
        take_screenshot(region,(last_x,last_y))  # final screenshot after filling
        # pyautogui.press("escape")
        # time.sleep(1)
        # cx = region["x"] + region["width"]  // 2
        # cy = region["y"] + region["height"] // 2
        # pyautogui.scroll(-8, x=cx, y=cy)
        pyautogui.scroll(-8)  # scroll down
        # time.sleep(1)
        print(f"  Scroll complete.")
        take_screenshot(region,(safe_x,safe_y))  # final screenshot after scrolling

    logger.info(f"Done. Filled: {sorted(all_filled)}")
    return len(all_filled) > 0

# ─────────────────────────────────────────────────────────────────────────────
# Bulk fill — fast pyautogui loop, 2 passes per screen-full (text inputs, then dropdowns), no more API calls
# ─────────────────────────────────────────────────────────────────────────────

def get_dropdown_coords(bedrock_client, screenshot_bytes: bytes, json_data: dict) -> dict:
    prompt = (
        "You are looking at a screenshot of a form.\n\n"

        "Your job:\n"
        "Identify ONLY visible dropdown fields whose label matches one of the keys in this list:\n"
        f"{json.dumps(json_data, indent=2)}\n\n"

        "- Return ONLY  the form field label, the entire json path for the matching key, and coordinates.\n"
        "- If the data value is numeric (e.g., premium, amount, totals), it is NOT a dropdown match.\n"
        "- Only include a dropdown if you can clearly explain (to yourself) why the label and key refer to the same concept.\n"
        "- If field does not match any key, skip it.\n\n"

        "Format:\n"
        '{\n'
        '  "State": {"x": 1157, "y": 419, "key_path": "insured state"},\n'
        '  "Line of Business": {"x": 350, "y": 584, "key_path": "type of coverage"}\n'
        '}'
    )

    response = bedrock_client.converse(
        modelId=MODEL_ID,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": screenshot_bytes}}},
                {"text": prompt},
            ],
        }],
    )

    raw = response["output"]["message"]["content"][0]["text"]
    ddls = extract_json(raw)
    print(f"Claude dropdown response: {ddls}")
    return ddls

def ddl_fill(bedrock_client, ddl_dict: dict, json_data: dict, region: dict) -> set:
    filled = set()
    for label, info in ddl_dict.items():
        abs_x = int(info["x"]) + region["x"]
        abs_y = int(info["y"] * 1.05) + region["y"]

        pyautogui.click(abs_x, abs_y)
        # time.sleep(0.4)

        print(f"  screenshot for claude after opening dropdown '{label}'")
        screenshot = take_screenshot(region)

        prompt = (
            "You are looking at an OPEN dropdown list.\n\n"
            f"Target value: {json.dumps(json_data)}\n\n"
            # "Click the best matching visible option.\n\n"
            "Return ONLY:\n"
            '{"x": ..., "y": ..., "value": "..."}'
        )

        response = bedrock_client.converse(
            modelId=MODEL_ID,
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": screenshot}}},
                    {"text": prompt},
                ],
            }],
        )

        result = extract_json(response["output"]["message"]["content"][0]["text"])

        opt_x = int(result["x"]) + region["x"]
        opt_y = int(result["y"] * 1.05) + region["y"]
        print(f"  clicking option at ({opt_x},{opt_y}) for field '{label}'")
        pyautogui.click(abs_x, opt_y) # why does x drift?
        # time.sleep(2)
        print(f"filled ddl field '{label}' with value: {result['value']}")
        take_screenshot(region,(abs_x, opt_y))  # final screenshot after filling
        filled.add(label)
        print("\n")

def get_tb_coords(bedrock_client, screenshot_bytes: bytes,
                          json_data: dict, already_filled: set) -> dict:
    """
    Send one screenshot + the full job data to Claude.

    Claude figures out:
      - Which visible fields match which data values
      - Where each field is (pixel coordinates)
      - How to format each value (dates, currency, state abbreviations, etc.)

    Returns a ready-to-execute dict:
      {
        "Insured Name":   {"x": 630, "y": 354, "value": "Acme Corp LLC"},
        "Effective Date": {"x": 322, "y": 727, "value": "01/15/2025"},
        ...
        "__has_more_fields__": false
      }

    already_filled: set of field labels filled in previous scroll passes,
                    so Claude skips them and focuses on new ones.
    """
    skip_note = ""
    if already_filled:
        skip_note = (
            f"\nFields already filled in a previous pass (skip these): "
            f"{sorted(already_filled)}\n"
        )

    prompt = (
        "You are looking at a screenshot of an insurance AMS "
        "(Agency Management System) form.\n\n"

        "Here is data available to fill this form — use what matches, ignore what doesn't:\n"
        f"{json.dumps(json_data, indent=2)}\n\n"

        "Your job:\n"
        "1. Look at every visible, editable TEXT INPUT or DATE field in the screenshot.\n"
        "2. Match available data to fields using common sense.\n"
        "3. Return a JSON object for every field you can confidently fill.\n\n"

        "STRICT RULES:\n"
        "- ONLY include text inputs and date fields.\n"
        "- DO NOT include dropdowns or select fields.\n"
        "- DO NOT guess values.\n"
        "- DO NOT include fields unless you are confident.\n"
        "- Broker field on the form is likely referring to the wholesale broker listed in the data.\n"
        "- Skip fields already filled.\n\n"

        "Formatting rules:\n"
        "- Dates → MM/DD/YYYY\n"
        "- Currency → digits only (no $)\n"
        "- State → 2-letter abbreviation\n"
        "- Phone → (555) 000-0000 if possible\n\n"

        f"{skip_note}"

        "Return ONLY valid JSON. No explanation.\n"
        "Format:\n"
        '{\n'
        '  "Insured Name":     {"x": 630, "y": 354, "value": "Acme Corp LLC"},\n'
        '  "Effective Date":   {"x": 322, "y": 727, "value": "02/10/2026"}\n'
        '}'
    )
    response = bedrock_client.converse(
        modelId=MODEL_ID,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": "png", "source": {"bytes": screenshot_bytes}}},
                {"text": prompt},
            ],
        }],
    )

    raw = response["output"]["message"]["content"][0]["text"]
    logger.info(f"Claude pass1 response ({len(raw)} chars): {raw!r}")
    return extract_json(raw)


def tb_fill(tb_dict: dict, region: dict) -> set:
    filled = set()
    # Click somewhere safe first to ensure browser address bar isn't focused
    safe_x = region["x"] + region["width"] // 2
    safe_y = region["y"] + region["height"] // 2
    pyautogui.click(safe_x, safe_y)
    # time.sleep(0.1)
    pyautogui.press("escape")   # dismiss any dropdowns/autocomplete
    # time.sleep(0.1)
    
    print(f"\n  Filling from quote : {tb_dict} \n")
    for path, info in flatten_with_path(tb_dict):
        label = path.split(".")[-1]
        # Skip metadata keys
        if label.startswith("__") or not isinstance(info, dict):
            continue
        value = str(info.get("value", "")).strip()
        if not value:
            logger.debug(f"Skipping '{label}' — no value")
            continue
        abs_x = int(info.get("x", 0)) + region["x"]
        abs_y = int(info["y"] * 1.05) + region["y"] #todo: remove this hack..use textbox handles?
        try:
            # time.sleep(1)
            print(f"\n clicking field '{label}' at ({abs_x},{abs_y}) to fill value: {value}")
            take_screenshot(region,(abs_x,abs_y))  # final screenshot after filling
            pyautogui.click(abs_x, abs_y)
            # time.sleep(1)
            # logger.info(f"clicked field '{label}'")
            # take_screenshot(region, (abs_x, abs_y))
            pyperclip.copy(value)
            logger.info(f"FILLING FIELD: json Path: {path} Value: {value} Coords: ({abs_x},{abs_y})")
            pyautogui.hotkey(*PASTE_HOTKEY)    # paste
            # time.sleep(1)
            print(f"filled field '{label}' with value: {value}")
            take_screenshot(region,(abs_x,abs_y))  # final screenshot after filling
            filled.add(label)
            # check for successful paste by taking another screenshot and looking for the value? log success and remove from json
        except Exception as e:
            logger.warning(f"  Failed to fill '{label}' at ({abs_x},{abs_y}): {e}")
    return filled

# ─────────────────────────────────────────────────────────────────────────────
# Overlay widget — drag onto AMS window, click Push
# ─────────────────────────────────────────────────────────────────────────────

def show_overlay_and_wait() -> Optional[tuple]:
    """
    Shows a draggable always-on-top widget.
    User drags it onto the AMS window and clicks "Push Data Here".
    Returns (x, y) center of the widget when clicked, or None if cancelled.
    Must run on the main thread (macOS tkinter requirement).
    """
    import tkinter as tk
    result = {"pos": None}

    root = tk.Tk()
    root.title("AMS Agent")
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    root.attributes("-alpha", 0.95)
    root.configure(bg="#1a1f2e")
    root.resizable(False, False)

    w, h     = 220, 160
    screen_w = root.winfo_screenwidth()
    root.geometry(f"{w}x{h}+{screen_w - w - 20}+80")

    drag = {"x": 0, "y": 0}

    def drag_start(e):
        drag["x"] = e.x_root - root.winfo_x()
        drag["y"] = e.y_root - root.winfo_y()

    def drag_move(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    # Header / drag handle
    hdr = tk.Frame(root, bg="#141824", cursor="fleur")
    hdr.pack(fill="x")
    hdr.bind("<ButtonPress-1>", drag_start)
    hdr.bind("<B1-Motion>", drag_move)

    inner = tk.Frame(hdr, bg="#141824")
    inner.pack(fill="x", padx=12, pady=8)
    inner.bind("<ButtonPress-1>", drag_start)
    inner.bind("<B1-Motion>", drag_move)

    for text, font, fg, side in [
        ("AMS Agent", ("Courier", 11, "bold"), "#4f8ef7", "left"),
        ("●",         ("Courier", 8),           "#2ecc8a", "right"),
        ("ready",     ("Helvetica", 9),         "#5a6180", "right"),
    ]:
        lbl = tk.Label(inner, text=text, font=font, fg=fg, bg="#141824")
        lbl.pack(side=side, padx=(0 if side != "right" else 4))
        lbl.bind("<ButtonPress-1>", drag_start)
        lbl.bind("<B1-Motion>", drag_move)

    # Body
    body = tk.Frame(root, bg="#1a1f2e")
    body.pack(fill="both", expand=True, padx=12, pady=6)

    tk.Label(
        body,
        text="Drag onto AMS window\nthen click below.",
        font=("Helvetica", 10), fg="#8892b0", bg="#1a1f2e",
        justify="center", wraplength=180,
    ).pack(pady=(4, 10))

    def on_push():
        cx = root.winfo_x() + root.winfo_width()  // 2
        cy = root.winfo_y() + root.winfo_height() // 2
        result["pos"] = (cx, cy)
        root.destroy()

    tk.Button(
        body,
        text="Push Data Here",
        font=("Helvetica", 11, "bold"), fg="#ffffff", bg="#4f8ef7",
        activebackground="#3a7ee8", activeforeground="#ffffff",
        relief="flat", cursor="hand2", padx=10, pady=8,
        command=on_push,
    ).pack(fill="x")

    cancel = tk.Label(
        body, text="cancel",
        font=("Helvetica", 9), fg="#3a4060", bg="#1a1f2e", cursor="hand2",
    )
    cancel.pack(pady=(6, 0))
    cancel.bind("<Button-1>", lambda e: root.destroy())

    root.configure(highlightbackground="#4f8ef7", highlightthickness=1)
    logger.info("Overlay shown — waiting for user to position and click...")
    root.mainloop()
    return result["pos"]


def prompt_user_to_select_window() -> Optional[dict]:
    """Show overlay, wait for click, return the window region dict."""
    pos = show_overlay_and_wait()
    if pos is None:
        logger.info("User cancelled")
        return None

    x, y   = pos
    region = _get_window_region_at(x, y)
    print("\n  Window selected — Claude is starting now!\n")
    return region


def _get_window_region_at(x: int, y: int) -> dict:
    """Return bounding box of the window at screen position (x, y)."""

    # macOS via Quartz
    try:
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        for win in windows:
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
        hwnd = win32gui.WindowFromPoint((x, y))
        if hwnd:
            wx, wy, wx2, wy2 = win32gui.GetWindowRect(hwnd)
            title  = win32gui.GetWindowText(hwnd)
            region = {"x": wx, "y": wy, "width": wx2 - wx, "height": wy2 - wy}
            logger.info(f"Window (Windows): '{title}' at {region}")
            return region
    except ImportError:
        pass

    # Fallback: full screen
    logger.warning("Could not detect window bounds — using full screen")
    w, h = pyautogui.size()
    return {"x": 0, "y": 0, "width": w, "height": h}


# ─────────────────────────────────────────────────────────────────────────────
# Flask communication
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Job runner
# ─────────────────────────────────────────────────────────────────────────────

def run_job(job: dict, server_url: str, bedrock_client):
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

    region = prompt_user_to_select_window()
    if region is None:
        update_job_status(server_url, job_id, "failed", "User cancelled")
        return

    logger.info(f"Target region: {region}")

    try:
        success = run_vision_job(bedrock_client, json_data, region)
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


# ─────────────────────────────────────────────────────────────────────────────
# Polling thread
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AMS Export Agent — vision-map")
    parser.add_argument("--server",     default=DEFAULT_SERVER_URL,
                        help=f"Flask server URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--aws-region", default=AWS_REGION,
                        help=f"AWS region (default: {AWS_REGION})")
    args       = parser.parse_args()
    server_url = args.server.rstrip("/")

    print(f"""
  AMS Export Agent
  ─────────────────────────────────
  Agent ID : {AGENT_ID}
  Server   : {server_url}
  OS       : {platform.system()}
  Paste    : {'+'.join(PASTE_HOTKEY)}
    """)

    try:
        bedrock_client = boto3.client("bedrock-runtime", region_name=args.aws_region)
        logger.info(f"Bedrock ready (region={args.aws_region})")
    except Exception as e:
        logger.error(f"Bedrock init failed: {e}")
        sys.exit(1)

    # Polling runs in background; tkinter must stay on main thread
    threading.Thread(
        target=polling_loop, args=(server_url,), daemon=True
    ).start()
    logger.info(f"Polling {server_url} every {POLL_INTERVAL}s...")
    print("Waiting for jobs — Ctrl+C to stop.\n")

    while True:
        try:
            job = job_queue.get(timeout=0.5)
            run_job(job, server_url, bedrock_client)
            job_queue.task_done()
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            print("\nAgent stopped.")
            sys.exit(0)


if __name__ == "__main__":
    main()