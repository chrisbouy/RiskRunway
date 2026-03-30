#!/usr/bin/env python3
"""
AMS Export Agent - Local computer use automation
Runs on user's machine, polls Flask server for jobs, uses Bedrock Computer Use.

Usage:
    python local_agent.py
    python local_agent.py --server http://192.168.1.100:5000
    python local_agent.py --server http://mycompany.intranet:5000
"""

import json
import time
import uuid
import logging
import argparse
import tempfile
import threading
import queue
import sys
import io
from pathlib import Path

import boto3
import pyperclip
import requests
import pyautogui

# ── mss replaces PIL ImageGrab — handles negative y on multi-monitor macOS ──
try:
    import mss
    import mss.tools
    from PIL import Image
    USE_MSS = True
except ImportError:
    from PIL import ImageGrab, Image
    USE_MSS = False

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
DEFAULT_SERVER_URL = "http://localhost:5000"
AGENT_ID           = str(uuid.uuid4())[:8]
POLL_INTERVAL      = 0.5        # Seconds between polls when idle
MAX_TURNS          = 30         # Max Claude turns per job (safety limit)
ACTION_DELAY       = 0.8        # Seconds to wait after each action
SCREENSHOT_PATH    = Path(tempfile.gettempdir()) / "ams_screenshot.png"

AWS_REGION = "us-east-1"
MODEL_ID   = "us.anthropic.claude-sonnet-4-20250514-v1:0"

logging.basicConfig(
    level=logging.INFO,
    format=f"[Agent {AGENT_ID}] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

if USE_MSS:
    logger.info("Screenshot backend: mss (multi-monitor safe)")
else:
    logger.warning("mss not installed — falling back to PIL ImageGrab (may fail on second monitor)")
    logger.warning("Fix: pip install mss")

# Global queue — polling thread puts jobs here, main thread processes them
job_queue = queue.Queue()


# ─────────────────────────────────────────────
# Screen Flash (confirmation feedback)
# ─────────────────────────────────────────────

def flash_screen():
    """
    Flash the screen white for 150ms to confirm window selection.
    Uses after() with a forced update loop to ensure it closes reliably.
    """
    try:
        import tkinter as tk
        root = tk.Tk()

        # Get full virtual desktop size (spans all monitors)
        total_w = root.winfo_screenwidth()
        total_h = root.winfo_screenheight()

        root.geometry(f"{total_w}x{total_h}+0+0")
        root.attributes('-topmost', True)
        root.attributes('-alpha', 0.4)
        root.configure(bg='white')
        root.overrideredirect(True)  # No title bar
        root.update()

        # Force close after 150ms — don't rely on mainloop
        root.after(150, lambda: root.quit())
        root.mainloop()
        root.destroy()

    except Exception as e:
        logger.warning(f"Screen flash failed: {e}")


# ─────────────────────────────────────────────
# Overlay Widget — draggable "Push Data Here"
# ─────────────────────────────────────────────

def show_overlay_and_wait():
    """
    Show a small always-on-top draggable overlay window.
    User drags it onto the AMS window, then clicks "Push Data Here".

    Returns:
        (x, y) tuple of where the button was clicked (center of overlay)
        or None if the user cancelled.

    Must be called from the main thread.
    """
    import tkinter as tk

    result = {"pos": None, "cancelled": False}

    root = tk.Tk()
    root.title("AMS Agent")
    root.attributes('-topmost', True)
    root.overrideredirect(True)           # Remove OS title bar
    root.attributes('-alpha', 0.95)
    root.configure(bg='#1a1f2e')
    root.resizable(False, False)

    # ── Window size & initial position (top-right corner) ──
    w, h = 220, 160
    screen_w = root.winfo_screenwidth()
    root.geometry(f"{w}x{h}+{screen_w - w - 20}+80")

    # ── Drag support ──
    drag_state = {"x": 0, "y": 0}

    def on_drag_start(event):
        drag_state["x"] = event.x_root - root.winfo_x()
        drag_state["y"] = event.y_root - root.winfo_y()

    def on_drag_motion(event):
        x = event.x_root - drag_state["x"]
        y = event.y_root - drag_state["y"]
        root.geometry(f"+{x}+{y}")

    # ── UI ──
    # Header bar (drag handle)
    header = tk.Frame(root, bg='#141824', cursor='fleur')
    header.pack(fill='x', padx=0, pady=0)
    header.bind('<ButtonPress-1>', on_drag_start)
    header.bind('<B1-Motion>', on_drag_motion)

    header_inner = tk.Frame(header, bg='#141824')
    header_inner.pack(fill='x', padx=12, pady=8)
    header_inner.bind('<ButtonPress-1>', on_drag_start)
    header_inner.bind('<B1-Motion>', on_drag_motion)

    title_lbl = tk.Label(
        header_inner, text="AMS Agent",
        font=('Courier', 11, 'bold'),
        fg='#4f8ef7', bg='#141824'
    )
    title_lbl.pack(side='left')
    title_lbl.bind('<ButtonPress-1>', on_drag_start)
    title_lbl.bind('<B1-Motion>', on_drag_motion)

    dot = tk.Label(header_inner, text="●", font=('Courier', 8), fg='#2ecc8a', bg='#141824')
    dot.pack(side='right')

    status_lbl = tk.Label(
        header_inner, text="ready",
        font=('Helvetica', 9), fg='#5a6180', bg='#141824'
    )
    status_lbl.pack(side='right', padx=4)

    # Body
    body = tk.Frame(root, bg='#1a1f2e')
    body.pack(fill='both', expand=True, padx=12, pady=6)

    instruction = tk.Label(
        body,
        text="Drag me onto your AMS\nwindow, then click below.",
        font=('Helvetica', 10),
        fg='#8892b0', bg='#1a1f2e',
        justify='center',
        wraplength=180
    )
    instruction.pack(pady=(4, 10))

    # Push Data button
    def on_push():
        # Capture center of the overlay window as the target point
        cx = root.winfo_x() + root.winfo_width() // 2
        cy = root.winfo_y() + root.winfo_height() // 2
        result["pos"] = (cx, cy)
        root.destroy()

    push_btn = tk.Button(
        body,
        text="⚡  Push Data Here",
        font=('Helvetica', 11, 'bold'),
        fg='#ffffff', bg='#4f8ef7',
        activebackground='#3a7ee8',
        activeforeground='#ffffff',
        relief='flat', cursor='hand2',
        padx=10, pady=8,
        command=on_push
    )
    push_btn.pack(fill='x')

    # Cancel link
    def on_cancel():
        result["cancelled"] = True
        root.destroy()

    cancel_lbl = tk.Label(
        body, text="cancel",
        font=('Helvetica', 9),
        fg='#3a4060', bg='#1a1f2e',
        cursor='hand2'
    )
    cancel_lbl.pack(pady=(6, 0))
    cancel_lbl.bind('<Button-1>', lambda e: on_cancel())

    # ── Border effect (outer frame) ──
    root.configure(highlightbackground='#4f8ef7', highlightthickness=1)

    logger.info("Overlay shown — waiting for user to position and click 'Push Data Here'")
    root.mainloop()

    if result["cancelled"] or result["pos"] is None:
        return None

    return result["pos"]


# ─────────────────────────────────────────────
# Window Selection
# ─────────────────────────────────────────────

def prompt_user_to_select_window():
    """
    1. Show the draggable overlay widget.
    2. User drags it onto the AMS window and clicks "Push Data Here".
    3. Flash the screen white to confirm.
    4. Detect and return the window bounds at the click position.

    Must be called from the main thread.

    Returns:
        dict with keys: x, y, width, height
        or None if the user cancelled.
    """
    pos = show_overlay_and_wait()

    if pos is None:
        logger.info("User cancelled overlay")
        return None

    x, y = pos
    logger.info(f"Overlay clicked at ({x}, {y})")

    # Flash screen to confirm selection
    # flash_screen()

    # Detect window bounds at that position
    region = _get_window_region_at(x, y)
    print(f"✅  Window selected — Claude is starting now!\n")
    return region


def _get_window_region_at(x, y):
    """
    Get the bounding box of the window at screen coordinates (x, y).
    Tries Windows, macOS, and Linux in order. Falls back to full screen.
    """
    # ── Windows (pywin32) ──
    try:
        import win32gui
        hwnd = win32gui.WindowFromPoint((x, y))
        if hwnd:
            rect = win32gui.GetWindowRect(hwnd)
            wx, wy, wx2, wy2 = rect
            region = {
                "x": max(0, wx),
                "y": max(0, wy),
                "width": wx2 - wx,
                "height": wy2 - wy
            }
            title = win32gui.GetWindowText(hwnd)
            logger.info(f"Selected window: '{title}' at {region}")
            return region
    except ImportError:
        pass

    # ── macOS (Quartz) ──
    try:
        from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        for win in window_list:
            bounds = win.get("kCGWindowBounds", {})
            wx  = int(bounds.get("X", 0))
            wy  = int(bounds.get("Y", 0))
            ww  = int(bounds.get("Width", 0))
            wh  = int(bounds.get("Height", 0))
            if wx <= x <= wx + ww and wy <= y <= wy + wh:
                region = {"x": wx, "y": wy, "width": ww, "height": wh}
                logger.info(f"Selected window (macOS) at {region}")
                return region
    except ImportError:
        pass

    # ── Linux (xdotool) ──
    try:
        import subprocess
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            geo = {}
            for line in result.stdout.splitlines():
                k, _, v = line.partition("=")
                geo[k.strip()] = v.strip()
            region = {
                "x": int(geo.get("X", 0)),
                "y": int(geo.get("Y", 0)),
                "width": int(geo.get("WIDTH", 1920)),
                "height": int(geo.get("HEIGHT", 1080))
            }
            logger.info(f"Selected window (Linux) at {region}")
            return region
    except Exception:
        pass

    logger.warning("Could not detect window bounds — using full screen")
    return _get_full_screen_region()


def _get_full_screen_region():
    """Return the full screen as the target region."""
    width, height = pyautogui.size()
    return {"x": 0, "y": 0, "width": width, "height": height}


# ─────────────────────────────────────────────
# Screenshot
# ─────────────────────────────────────────────

def take_screenshot(region=None):
    """
    Capture a screenshot of the given region and return raw PNG bytes.

    Uses mss when available — it handles negative y coordinates correctly
    on macOS multi-monitor setups (e.g. a second monitor positioned above
    the primary where y origin is negative).

    Falls back to PIL ImageGrab if mss is not installed.
    """
    if USE_MSS:
        raw_bytes = _screenshot_mss(region)
    else:
        raw_bytes = _screenshot_pil(region)

    # ── Debug: save a copy so we can inspect what Claude sees ──
    debug_path = Path(tempfile.gettempdir()) / "ams_debug_last.png"
    debug_path.write_bytes(raw_bytes)

    # Log dimensions by re-opening (cheap — just reads header)
    img = Image.open(io.BytesIO(raw_bytes))
    logger.info(f"Screenshot saved for inspection: {debug_path} ({img.width}x{img.height})")

    return raw_bytes


def _screenshot_mss(region=None):
    """Capture using mss — handles negative coords on macOS multi-monitor."""
    with mss.mss() as sct:
        if region:
            monitor = {
                "left":   region["x"],
                "top":    region["y"],      # mss handles negative y correctly
                "width":  region["width"],
                "height": region["height"],
            }
        else:
            # Full virtual desktop (all monitors combined)
            monitor = sct.monitors[0]

        shot = sct.grab(monitor)

        # mss returns BGRA — convert to RGB for PIL, then encode as PNG
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _screenshot_pil(region=None):
    """Fallback capture using PIL ImageGrab (may fail on negative-y monitors)."""
    if region:
        bbox = (
            region["x"],
            region["y"],
            region["x"] + region["width"],
            region["y"] + region["height"],
        )
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────
# Action Execution
# ─────────────────────────────────────────────

def execute_action(action, region=None):
    """
    Execute a single Computer Use action using pyautogui.
    Coordinates from Claude are relative to the region's top-left corner.
    We offset them to get absolute screen coordinates.
    """
    action_type = action.get("action", "")
    offset_x    = region["x"] if region else 0
    offset_y    = region["y"] if region else 0

    def abs_xy(coord_key="coordinate"):
        """Return absolute screen coords from a region-relative coordinate pair."""
        coords = action.get(coord_key, [0, 0])
        return coords[0] + offset_x, coords[1] + offset_y

    # ── Click variants ──────────────────────────────────────────────────────
    if action_type in ("left_click", "click", "mouse"):
        x, y = abs_xy()
        pyautogui.click(x, y)
        logger.info(f"Left click at ({x}, {y})")

    elif action_type == "right_click":
        x, y = abs_xy()
        pyautogui.rightClick(x, y)
        logger.info(f"Right click at ({x}, {y})")

    elif action_type == "double_click":
        x, y = abs_xy()
        pyautogui.doubleClick(x, y)
        logger.info(f"Double click at ({x}, {y})")

    elif action_type == "middle_click":
        x, y = abs_xy()
        pyautogui.click(x, y, button="middle")
        logger.info(f"Middle click at ({x}, {y})")

    elif action_type == "move":
        x, y = abs_xy()
        pyautogui.moveTo(x, y)
        logger.info(f"Move to ({x}, {y})")

    # ── Keyboard ────────────────────────────────────────────────────────────
    elif action_type == "type":
        text = action.get("text", "")
        pyperclip.copy(text)
        pyautogui.hotkey("command", "v")   # macOS paste
        logger.info(f"Pasted text ({len(text)} chars): {text[:40]!r}")

    elif action_type == "key":
        # Claude sends key names in action["text"], not action["key"]
        key = action.get("text") or action.get("key", "")
        key_map = {
            "Return":    "enter",
            "Tab":       "tab",
            "Escape":    "esc",
            "escape":    "esc",
            "BackSpace": "backspace",
            "Delete":    "delete",
            "super+tab": ["alt", "tab"],   # macOS app switcher
            "ctrl+a":    ["ctrl", "a"],
            "ctrl+c":    ["ctrl", "c"],
            "ctrl+v":    ["ctrl", "v"],
            "ctrl+z":    ["ctrl", "z"],
            "cmd+a":     ["command", "a"],
            "cmd+c":     ["command", "c"],
            "cmd+v":     ["command", "v"],
            "cmd+z":     ["command", "z"],
        }
        mapped = key_map.get(key, key)
        if isinstance(mapped, list):
            pyautogui.hotkey(*mapped)
        else:
            pyautogui.press(mapped)
        logger.info(f"Key press: {key!r} → {mapped!r}")

    # ── Scroll ──────────────────────────────────────────────────────────────
    elif action_type == "scroll":
        x, y         = abs_xy()
        direction    = action.get("direction", "down")
        amount       = action.get("amount", 3)
        scroll_delta = amount if direction == "up" else -amount
        pyautogui.scroll(scroll_delta, x=x, y=y)
        logger.info(f"Scroll {direction} {amount} at ({x}, {y})")

    # ── Drag ────────────────────────────────────────────────────────────────
    elif action_type == "drag":
        sx, sy = abs_xy("startCoordinate")
        ex, ey = abs_xy("coordinate")
        pyautogui.mouseDown(sx, sy)
        pyautogui.moveTo(ex, ey, duration=0.5)
        pyautogui.mouseUp()
        logger.info(f"Drag ({sx},{sy}) → ({ex},{ey})")

    # ── Screenshot — handled by the loop, not here ──────────────────────────
    elif action_type == "screenshot":
        pass

    else:
        logger.warning(f"Unknown action type: {action_type!r} — skipping")
        logger.warning(f"Full action dict: {action}")

# ─────────────────────────────────────────────
# Bedrock Computer Use — Agentic Loop
# ─────────────────────────────────────────────

def run_computer_use_loop(bedrock_client, json_data, region):
    """
    Core agentic loop:
      1. Take screenshot of selected region
      2. Send to Claude with instructions + data
      3. Execute returned actions
      4. Feed updated screenshot back to Claude
      5. Repeat until Claude stops issuing tool calls or MAX_TURNS reached

    Returns True on success, False on failure.
    """

    messages = []

    # Initial screenshot
    screenshot_bytes = take_screenshot(region)

    messages.append({
        "role": "user",
        "content": [
            {
                "image": {
                    "format": "png",
                    "source": {"bytes": screenshot_bytes}
                }
            },
            {
                "text": (
                    "You are controlling a computer using the provided tools.\n\n"
                    "Task:\n"
                    "Look at the screenshot of the AMS window.\n"
                    "Type the text \"abc\" into every visible input field in the UI.\n\n"
                    "Rules:\n"
                    "- Click into each field before typing\n"
                    "- If fields are off-screen, scroll to find them\n"
                    "- Continue until all visible fields contain \"abc\"\n"
                    "- Do not stop early\n"
                    "- You MUST use the computer tool to complete this task\n"
                )
            }
        ]
    })

    for turn in range(MAX_TURNS):
        logger.info(f"Turn {turn + 1}/{MAX_TURNS}")

        try:
            response = bedrock_client.converse(
                modelId=MODEL_ID,
                system=[{
                    "text": "You are controlling a computer using the computer tool."
                }],
                messages=messages,
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": "computer",
                                "description": "Control mouse, keyboard, and screenshot the screen",
                                "inputSchema": {
                                    "json": {
                                        "type": "object",
                                        "properties": {
                                            "action": {
                                                "type": "string",
                                                "enum": ["screenshot", "mouse", "type", "key", "scroll"]
                                            },
                                            "coordinate": {"type": "array", "items": {"type": "integer"}},
                                            "text": {"type": "string"},
                                            "direction": {"type": "string"},
                                            "amount": {"type": "integer"}
                                        },
                                        "required": ["action"]
                                    }
                                }
                            }
                        }
                    ]
                },
                additionalModelRequestFields={
                    "anthropic_beta": ["computer-use-2025-01-24"]
                }
            )
          
            
            
            
        except Exception as e:
            logger.error(f"Bedrock call failed on turn {turn + 1}: {e}")
            return False

        # Debug full response
        print(json.dumps(response, indent=2, default=str))

        output_message = response["output"]["message"]
        messages.append(output_message)

        content_blocks = output_message.get("content", [])

        # ---- Parse tool uses safely ----
        tool_uses = []
        for block in content_blocks:
            if "toolUse" in block:
                tool_uses.append(block["toolUse"])

        # ---- Log text output ----
        for block in content_blocks:
            if block.get("type") == "text" and block.get("text"):
                logger.info(f"Claude: {block['text'][:200]}")

        # ---- Safety check (prevents Bedrock validation crash later) ----
        if response.get("stopReason") == "tool_use" and not tool_uses:
            raise RuntimeError("Claude requested tool use but none were parsed.")

        # ---- Exit condition ----
        if not tool_uses:
            logger.info("Claude finished — no more actions requested")
            return True

        # ---- Execute tool calls ----
        tool_results = []

        for tu in tool_uses:
            tool_name = tu.get("name")
            tool_id = tu.get("toolUseId")
            tool_input = tu.get("input", {})

            logger.info(f"Tool: {tool_name} | action: {tool_input.get('action')}")

            if tool_name != "computer":
                logger.warning(f"Unknown tool: {tool_name}")
                continue

            action_type = tool_input.get("action")

            try:
                if action_type == "screenshot":
                    new_screenshot = take_screenshot(region)
                else:
                    execute_action(tool_input, region=region)
                    time.sleep(ACTION_DELAY)
                    new_screenshot = take_screenshot(region)

                tool_results.append({
                    "toolResult": {
                        "toolUseId": tool_id,
                        "content": [
                            {
                                "image": {
                                    "format": "png",
                                    "source": {"bytes": new_screenshot}
                                }
                            }
                        ]
                    }
                })

            except Exception as e:
                logger.error(f"Tool execution failed: {e}")
                return False

        # ---- CRITICAL: respond immediately with tool_result ----
        messages.append({
            "role": "user",
            "content": tool_results
        })

        # loop continues automatically

    logger.error(f"Exceeded MAX_TURNS ({MAX_TURNS}) — job incomplete")
    return False
# ─────────────────────────────────────────────
# Flask Server Communication
# ─────────────────────────────────────────────

def poll_for_job(server_url):
    """Poll the Flask server for the next pending job. Returns job dict or None."""
    try:
        response = requests.get(
            f"{server_url}/api/ams/jobs/next",
            timeout=5
        )
        if response.status_code == 200:
            return response.json().get("job")
    except requests.exceptions.ConnectionError:
        logger.warning(f"Cannot reach server at {server_url} — retrying...")
    except Exception as e:
        logger.warning(f"Poll error: {e}")
    return None


def update_job_status(server_url, job_id, status, message=None):
    """Report job completion or failure back to the Flask server."""
    payload = {"status": status}
    if message:
        payload["message"] = message
    try:
        requests.patch(
            f"{server_url}/api/ams/jobs/{job_id}/status",
            json=payload,
            timeout=5
        )
        logger.info(f"Job {job_id} marked as {status}")
    except Exception as e:
        logger.error(f"Failed to update job {job_id} status: {e}")


# ─────────────────────────────────────────────
# Job Runner
# ─────────────────────────────────────────────

def run_job(job, server_url, bedrock_client):
    """
    Full lifecycle for one AMS export job:
      1. Show draggable overlay — user positions it and clicks Push Data Here
      2. Screen flashes white to confirm
      3. Claude runs the computer use loop
      4. Report success or failure back to server
    """
    job_id    = job["id"]
    json_data = job["json_data"]

    print(f"\n{'='*50}")
    print(f"  New AMS Export Job #{job_id}")
    print(f"{'='*50}\n")
    logger.info(f"Starting job {job_id}")

    region = prompt_user_to_select_window()

    if region is None:
        logger.info("User cancelled")
        update_job_status(server_url, job_id, "failed", "User cancelled")
        return

    logger.info(f"Target region: {region}")

    try:
        success = run_computer_use_loop(bedrock_client, json_data, region)
        if success:
            update_job_status(server_url, job_id, "complete")
            print(f"\n✅  Job #{job_id} completed successfully!")
        else:
            update_job_status(server_url, job_id, "failed", "Exceeded max turns")
            print(f"\n❌  Job #{job_id} failed — max turns exceeded")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        update_job_status(server_url, job_id, "failed", str(e))
        print(f"\n❌  Job #{job_id} failed: {e}")


# ─────────────────────────────────────────────
# Polling Thread
# ─────────────────────────────────────────────

def polling_loop(server_url):
    """
    Runs in a background thread.
    Polls Flask and puts jobs onto job_queue for the main thread to process.
    Waits (join) until the current job finishes before polling again.
    """
    while True:
        try:
            job = poll_for_job(server_url)
            if job:
                job_queue.put(job)
                job_queue.join()
            else:
                time.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AMS Export Agent")
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER_URL,
        help=f"Flask server URL (default: {DEFAULT_SERVER_URL})"
    )
    parser.add_argument(
        "--aws-region",
        default=AWS_REGION,
        help=f"AWS region for Bedrock (default: {AWS_REGION})"
    )
    args = parser.parse_args()
    server_url = args.server.rstrip("/")

    print(f"""
╔══════════════════════════════════════╗
║       AMS Export Agent               ║
║  ID:     {AGENT_ID:<28} ║
║  Server: {server_url:<28} ║
╚══════════════════════════════════════╝
    """)

    try:
        bedrock_client = boto3.client("bedrock-runtime", region_name=args.aws_region)
        logger.info(f"Bedrock client ready (region: {args.aws_region})")
    except Exception as e:
        logger.error(f"Failed to initialize Bedrock client: {e}")
        sys.exit(1)

    # Start polling in background thread
    poller = threading.Thread(target=polling_loop, args=(server_url,), daemon=True)
    poller.start()
    logger.info(f"Polling {server_url} every {POLL_INTERVAL}s...")
    print("Waiting for jobs — press Ctrl+C to stop.\n")

    # Main thread processes jobs (required for macOS tkinter)
    while True:
        try:
            job = job_queue.get(timeout=0.5)
            run_job(job, server_url, bedrock_client)
            job_queue.task_done()
        except queue.Empty:
            continue
        except KeyboardInterrupt:
            print("\n\nAgent stopped.")
            sys.exit(0)


if __name__ == "__main__":
    main()
