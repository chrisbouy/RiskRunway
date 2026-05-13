#!/usr/bin/env python3
"""
RiskRunway Launcher - macOS Protocol Handler
Properly handles riskrunway:// URLs using PyObjC and Apple Events
"""

import sys
import os
import subprocess
import urllib.parse
import urllib.request
import json
import logging
from pathlib import Path

# Setup logging
LOG_FILE = "/tmp/riskrunway_launcher.log"
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Global to store URL from Apple Event
g_received_url = None

def log_system_info():
    """Log system information for debugging"""
    logger.info(f"Python: {sys.executable}")
    logger.info(f"Version: {sys.version}")
    logger.info(f"Args: {sys.argv}")
    logger.info(f"CWD: {os.getcwd()}")
    logger.info(f"UID: {os.getuid()}")

def get_app_directories():
    """Get application directories"""
    script_dir = Path(__file__).parent.absolute()
    contents_dir = script_dir.parent
    resources_dir = contents_dir / "Resources"
    return script_dir, contents_dir, resources_dir

def find_python_with_deps():
    """Find a Python interpreter that has the required dependencies"""
    # First, try to find the project virtual environment
    # Look relative to the app bundle
    _, contents_dir, _ = get_app_directories()
    
    # The app is at /Applications/RiskRunwayLauncher.app
    # Project might be at ~/code_base/IPFS Mapper
    possible_venvs = [
        # Relative to app bundle (if installed near project)
        contents_dir.parent.parent.parent / "myenv" / "bin" / "python",
        contents_dir.parent.parent.parent / "venv" / "bin" / "python",
        contents_dir.parent.parent.parent / ".venv" / "bin" / "python",
        # Common project locations
        Path.home() / "code_base" / "IPFS Mapper" / "myenv" / "bin" / "python",
        Path.home() / "RiskRunway" / "myenv" / "bin" / "python",
        # Current working directory venv
        Path(os.getcwd()).parent / "myenv" / "bin" / "python" if os.getcwd() else None,
    ]
    
    for venv_python in possible_venvs:
        if venv_python and venv_python.exists():
            logger.info(f"Found venv Python: {venv_python}")
            return str(venv_python)
    
    # Check if current Python has PIL
    try:
        import PIL
        logger.info(f"Using current Python (has PIL): {sys.executable}")
        return sys.executable
    except ImportError:
        pass
    
    # Fall back to system python3 and hope for the best
    logger.warning("No venv found, falling back to system python3")
    return "python3"

def find_agent_path(resources_dir):
    """Find local_agent.py in various locations"""
    # Try Resources first
    agent = resources_dir / "local_agent.py"
    if agent.exists():
        logger.info(f"Found agent in Resources: {agent}")
        return agent
    
    # Try parent project directory
    project_root = resources_dir.parent.parent  # .app/Contents/Resources -> .app -> parent
    agent = project_root / "local_agent.py"
    if agent.exists():
        logger.info(f"Found agent in project: {agent}")
        return agent
    
    # Try common locations
    locations = [
        Path.home() / "RiskRunway" / "local_agent.py",
        Path.home() / ".riskrunway" / "local_agent.py",
        Path("/Applications/RiskRunwayLauncher.app/Contents/Resources/local_agent.py"),
    ]
    for loc in locations:
        if loc.exists():
            logger.info(f"Found agent at: {loc}")
            return loc
    
    logger.error("Could not find local_agent.py in any location")
    return None

def show_error(message):
    """Show error dialog using osascript"""
    logger.error(f"Showing error: {message}")
    subprocess.run([
        "osascript", "-e",
        f'display dialog "{message}" buttons {{"OK"}} default button "OK" with icon stop'
    ])

def show_info(message):
    """Show info dialog using osascript"""
    logger.info(f"Showing info: {message}")
    subprocess.run([
        "osascript", "-e",
        f'display dialog "{message}" buttons {{"OK"}} default button "OK" with icon note'
    ])

# ─────────────────────────────────────────────────────────────────────────────
# Permission Checking
# ─────────────────────────────────────────────────────────────────────────────

PERMISSIONS_GRANTED_FLAG = Path.home() / ".riskrunway" / ".permissions_granted"


def permissions_previously_granted() -> bool:
    """Check if user has already gone through the permission setup."""
    return PERMISSIONS_GRANTED_FLAG.exists()


def mark_permissions_granted():
    """Mark that user has completed permission setup."""
    PERMISSIONS_GRANTED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PERMISSIONS_GRANTED_FLAG.write_text("ok")


def open_accessibility_settings():
    """Open System Settings to the Accessibility pane."""
    subprocess.run([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ])


def open_screen_recording_settings():
    """Open System Settings to the Screen Recording pane."""
    subprocess.run([
        "open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    ])


def show_first_run_dialog() -> bool:
    """
    Show a one-time setup dialog on first launch explaining permissions.
    Returns True if user clicks 'Open Settings', False if they cancel.
    """
    message = (
        "Welcome to RiskRunway AMS Export!\\n\\n"
        "Before first use, Terminal needs two permissions:\\n\\n"
        "1. Screen Recording — to see AMS form fields\\n"
        "2. Accessibility — to click and type into fields\\n\\n"
        "Click 'Open Settings' and enable both for Terminal.\\n"
        "You only need to do this once."
    )
    
    result = subprocess.run(
        ["osascript", "-e",
         f'display dialog "{message}" buttons {{"Cancel", "Open Settings"}} '
         f'default button "Open Settings" with title "RiskRunway — First Time Setup" with icon note'],
        capture_output=True, text=True
    )
    
    return result.returncode == 0


def ensure_permissions() -> bool:
    """
    On first launch, guide user through granting permissions.
    On subsequent launches, skip the check entirely.
    Returns True to proceed, False if user cancelled.
    """
    # If user already completed setup, skip entirely
    if permissions_previously_granted():
        logger.info("Permissions previously granted — skipping check")
        return True
    
    logger.info("First run — showing permission setup dialog")
    
    # Show the one-time dialog
    user_wants_to_proceed = show_first_run_dialog()
    if not user_wants_to_proceed:
        logger.info("User cancelled permission setup")
        return False
    
    # Open Accessibility settings first
    open_accessibility_settings()
    import time
    time.sleep(1)  # Give System Settings time to open
    
    subprocess.run([
        "osascript", "-e",
        'display dialog "Step 1 of 2:\\n\\nEnable Terminal in the Accessibility list, then click Next." '
        'buttons {"Next"} default button "Next" with title "RiskRunway Setup (1/2)" with icon note'
    ])
    
    # Now open Screen Recording settings
    open_screen_recording_settings()
    time.sleep(1)  # Give System Settings time to navigate
    
    subprocess.run([
        "osascript", "-e",
        'display dialog "Step 2 of 2:\\n\\nEnable Terminal in the Screen Recording list, then click Done." '
        'buttons {"Done"} default button "Done" with title "RiskRunway Setup (2/2)" with icon note'
    ])
    
    # Mark as done so we never ask again
    mark_permissions_granted()
    logger.info("Permission setup complete — marked as done")
    return True


def spawn_in_terminal(job_id, server_url, agent_path):
    """Spawn local_agent in a hidden Terminal window."""
    import shlex
    
    python = find_python_with_deps()
    python_quoted = shlex.quote(python)
    agent_quoted = shlex.quote(str(agent_path))
    server_quoted = shlex.quote(server_url)
    
    cmd = f'{python_quoted} {agent_quoted} --job-id {job_id} --server {server_quoted}'
    
    logger.info(f"Spawning agent with command: {cmd}")
    
    # Write the shell command to a temp script file
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
        script_path = f.name
        f.write('#!/bin/bash\n')
        f.write(f'{cmd}\n')
    
    os.chmod(script_path, 0o755)
    
    try:
        # Use AppleScript to open a new Terminal window, run the script, then hide Terminal
        applescript = (
            'tell application "Terminal"\n'
            f'    do script "{script_path}"\n'
            '    set miniaturized of front window to true\n'
            'end tell'
        )
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("Terminal opened and minimized successfully")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to open Terminal: {e}")
        logger.error(f"stderr: {e.stderr}")
        show_error(f"Could not launch agent. Error: {e.stderr}")
    finally:
        # Clean up temp file after a delay
        def cleanup():
            import time
            time.sleep(10)
            try:
                os.unlink(script_path)
            except:
                pass
        import threading
        threading.Thread(target=cleanup, daemon=True).start()

def handle_url(url):
    """Parse URL and spawn local_agent"""
    logger.info(f"Handling URL: {url}")
    
    # Handle both riskrunway:// and riskrunwaymapper:// URLs
    if url.startswith("riskrunwaymapper://"):
        path_and_query = url[21:]  # Remove 'riskrunwaymapper://'
    elif url.startswith("riskrunway://"):
        path_and_query = url[13:]  # Remove 'riskrunway://'
    else:
        show_error("Invalid URL format. Expected riskrunway:// or riskrunwaymapper://...")
        return 1
    
    # Extract path and query
    if '?' in path_and_query:
        path, query = path_and_query.split('?', 1)
    else:
        path, query = path_and_query, ''
    
    logger.info(f"Path: {path}, Query: {query}")
    
    # Parse query parameters
    params = urllib.parse.parse_qs(query)
    
    job_id = params.get('job_id', [None])[0]
    server = params.get('server', [None])[0]
    
    logger.info(f"Parsed params: job_id={job_id}, server={server}")
    
    if not job_id or not server:
        show_error("Missing job_id or server parameter in URL")
        return 1
    
    # Get directories and find agent
    _, _, resources_dir = get_app_directories()
    agent_path = find_agent_path(resources_dir)
    
    if not agent_path:
        show_error("Could not find local_agent.py. Please reinstall RiskRunway.")
        return 1
    
    # Check permissions before spawning
    if not ensure_permissions():
        return 1
    
    # Spawn agent
    spawn_in_terminal(job_id, server, agent_path)
    return 0

def run_with_pyobjc():
    """Run with PyObjC event loop to handle Apple Events"""
    try:
        import Foundation
        import AppKit
        import objc
        
        logger.info("Using PyObjC for Apple Event handling")
        
        class AppDelegate(Foundation.NSObject):
            def applicationDidFinishLaunching_(self, notification):
                logger.info("Application finished launching")
                
                # Check if we got a URL from the event
                global g_received_url
                if g_received_url:
                    logger.info(f"Have URL from event: {g_received_url}")
                    handle_url(g_received_url)
                    # Exit after handling
                    AppKit.NSApp.terminate_(None)
                else:
                    # Check command line args (for testing)
                    if len(sys.argv) > 1 and sys.argv[1].startswith("riskrunway://"):
                        handle_url(sys.argv[1])
                        AppKit.NSApp.terminate_(None)
                    else:
                        logger.warning("No URL provided via Apple Event or command line")
                        show_info("RiskRunwayLauncher is not meant to be run directly. Please click the Export button in the RiskRunway web app.")
                        AppKit.NSApp.terminate_(None)
            
            def applicationWillTerminate_(self, notification):
                logger.info("Application will terminate")
            
            def application_openURLs_(self, app, urls):
                """Handle URL open request from macOS"""
                logger.info(f"Received URLs: {urls}")
                for url in urls:
                    url_str = str(url.absoluteString())
                    logger.info(f"URL: {url_str}")
                    if url_str.startswith("riskrunway://"):
                        handle_url(url_str)
                AppKit.NSApp.terminate_(None)
        
        # Create and run application
        app = AppKit.NSApplication.sharedApplication()
        delegate = AppDelegate.alloc().init()
        app.setDelegate_(delegate)
        
        logger.info("Starting NSApplication main loop")
        app.run()
        return 0
        
    except ImportError as e:
        logger.error(f"PyObjC not available: {e}")
        return None
    except Exception as e:
        logger.exception(f"Error in PyObjC handler: {e}")
        return None

def check_event_file():
    """Check for URL in event file (alternative mechanism)"""
    event_file = Path("/tmp/riskrunway_event.txt")
    if event_file.exists():
        try:
            url = event_file.read_text().strip()
            logger.info(f"Found URL in event file: {url}")
            event_file.unlink()  # Delete after reading
            return url
        except Exception as e:
            logger.error(f"Error reading event file: {e}")
    return None

def fetch_pending_job(server_url):
    """Fetch the most recent pending job from the server"""
    try:
        req = urllib.request.Request(
            f"{server_url}/api/ams/jobs/next",
            headers={'Accept': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get('success') and data.get('job'):
                return data['job']
    except Exception as e:
        logger.error(f"Error fetching pending job: {e}")
    return None

def get_default_server_url():
    """Get default server URL from config file or environment"""
    # Check environment variable
    env_url = os.environ.get('RISKRUNWAY_SERVER')
    if env_url:
        return env_url.rstrip('/')
    
    # Check config file
    config_file = Path.home() / ".riskrunway" / "config.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            url = config.get('server_url')
            if url:
                return url.rstrip('/')
        except Exception as e:
            logger.error(f"Error reading config: {e}")
    
    # Default to localhost for testing
    return "http://localhost:5001"

def main():
    """Main entry point"""
    log_system_info()
    
    # Determine server URL
    server_url = None
    
    # Check if URL was passed as argument
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        logger.info(f"Argument received: {arg}")
        
        # Check if it's a riskrunway:// URL (macOS protocol handler)
        if arg.startswith("riskrunway://") or arg.startswith("riskrunwaymapper://"):
            logger.info("Protocol URL received, parsing parameters")
            # Parse the URL to extract job_id and server
            url = arg
            if url.startswith("riskrunwaymapper://"):
                path_and_query = url[21:]  # Remove 'riskrunwaymapper://'
            elif url.startswith("riskrunway://"):
                path_and_query = url[13:]  # Remove 'riskrunway://'
            else:
                path_and_query = url[13:]  # fallback
            
            # Extract query parameters
            if '?' in path_and_query:
                path, query = path_and_query.split('?', 1)
            else:
                path, query = path_and_query, ''
            
            params = urllib.parse.parse_qs(query)
            job_id = params.get('job_id', [None])[0]
            server = params.get('server', [None])[0]
            
            logger.info(f"Parsed from URL: job_id={job_id}, server={server}")
            
            if job_id and server:
                # Use the specific job and server from URL
                server_url = server.rstrip('/')
                logger.info(f"Using server from URL: {server_url}")
                
                # Check permissions before spawning
                if not ensure_permissions():
                    return 1
                
                # Find agent and spawn for specific job
                _, _, resources_dir = get_app_directories()
                agent_path = find_agent_path(resources_dir)
                if not agent_path:
                    show_error("Could not find local_agent.py")
                    return 1
                
                spawn_in_terminal(job_id, server_url, agent_path)
                return 0
            else:
                logger.warning("URL missing job_id or server, falling back to polling")
        
        # Check if it's a server URL
        elif arg.startswith("http://") or arg.startswith("https://"):
            server_url = arg.rstrip('/')
            logger.info(f"Server URL from command line: {server_url}")
    
    # Fall back to polling mode
    server_url = get_default_server_url()
    logger.info(f"Using default server: {server_url}")
    
    # Fetch pending job
    job = fetch_pending_job(server_url)
    if not job:
        show_error(f"No pending AMS export jobs found on {server_url}")
        return 1
    
    logger.info(f"Found job: {job['id']}")
    
    # Check permissions before spawning
    if not ensure_permissions():
        return 1
    
    # Find agent and spawn
    _, _, resources_dir = get_app_directories()
    agent_path = find_agent_path(resources_dir)
    if not agent_path:
        show_error("Could not find local_agent.py")
        return 1
    
    spawn_in_terminal(job['id'], server_url, agent_path)
    return 0

if __name__ == "__main__":
    # Try PyObjC first — this properly receives riskrunway:// URLs via Apple Events
    result = run_with_pyobjc()
    if result is not None:
        sys.exit(result)
    # Fall back to argv-based handling if PyObjC isn't available
    sys.exit(main())
