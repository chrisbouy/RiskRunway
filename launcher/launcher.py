#!/usr/bin/env python3
"""
RiskRunway Launcher - Protocol Handler for riskrunway://

Handles riskrunway:// URLs from the browser and spawns local_agent.py
with the appropriate job parameters.

Usage:
    python launcher.py riskrunway://export?job_id=123&server=https://app.riskrunway.com

Installation (macOS):
    1. Build app bundle: ./build_macos.sh
    2. Install: ./install_macos.sh
    
Installation (Windows):
    1. Build: ./build_windows.bat
    2. Install: ./install_windows.bat
"""

import sys
import os
import subprocess
import urllib.parse
import platform
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[RiskRunwayLauncher] %(asctime)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def parse_riskrunway_url(url: str) -> dict:
    """Parse a riskrunway:// URL and extract parameters."""
    # Strip protocol prefix
    if url.startswith('riskrunway://'):
        url = url[13:]  # Remove 'riskrunway://'
    elif url.startswith('riskrunway:'):
        url = url[11:]   # Remove 'riskrunway:'
    
    # Parse path and query string
    if '?' in url:
        path, query = url.split('?', 1)
    else:
        path, query = url, ''
    
    # Parse query parameters
    params = urllib.parse.parse_qs(query)
    
    # Convert single-value lists to scalars
    result = {'action': path}
    for key, values in params.items():
        result[key] = values[0] if len(values) == 1 else values
    
    return result


def get_local_agent_path() -> Path:
    """Find local_agent.py relative to launcher location."""
    # Launcher is in launcher/ directory, local_agent.py is in parent
    launcher_dir = Path(__file__).parent.absolute()
    project_root = launcher_dir.parent
    
    agent_path = project_root / 'local_agent.py'
    
    if agent_path.exists():
        return agent_path
    
    # Fallback: search in common locations
    search_paths = [
        Path.home() / 'RiskRunway' / 'local_agent.py',
        Path.home() / '.riskrunway' / 'local_agent.py',
        Path('/Applications/RiskRunway.app/Contents/Resources/local_agent.py'),
    ]
    
    for path in search_paths:
        if path.exists():
            return path
    
    return None


def spawn_local_agent(job_id: int, server_url: str) -> bool:
    """Spawn local_agent.py with the given job parameters."""
    agent_path = get_local_agent_path()
    
    if not agent_path:
        logger.error("Could not find local_agent.py")
        print("❌ Error: Could not find local_agent.py")
        print("Please ensure RiskRunway is installed correctly.")
        return False
    
    logger.info(f"Found local_agent.py at: {agent_path}")
    
    # Build command
    cmd = [
        sys.executable,  # Python interpreter
        str(agent_path),
        '--job-id', str(job_id),
        '--server', server_url,
    ]
    
    logger.info(f"Spawning: {' '.join(cmd)}")
    
    try:
        # Spawn in a new process group so it survives if launcher exits
        if platform.system() == 'Windows':
            subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # macOS/Linux: detach from parent
            subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        
        logger.info(f"local_agent started for job {job_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to spawn local_agent: {e}")
        print(f"❌ Error starting local_agent: {e}")
        return False


def handle_export(params: dict) -> bool:
    """Handle riskrunway://export URL."""
    job_id = params.get('job_id')
    server = params.get('server')
    
    if not job_id:
        logger.error("Missing job_id parameter")
        print("❌ Error: Missing job_id in URL")
        return False
    
    if not server:
        logger.error("Missing server parameter")
        print("❌ Error: Missing server in URL")
        return False
    
    try:
        job_id = int(job_id)
    except ValueError:
        logger.error(f"Invalid job_id: {job_id}")
        print(f"❌ Error: Invalid job_id: {job_id}")
        return False
    
    print(f"🚀 Starting RiskRunway Export for Job #{job_id}")
    print(f"   Server: {server}")
    print()
    
    return spawn_local_agent(job_id, server)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: launcher.py <riskrunway://...>")
        print()
        print("Example:")
        print("  launcher.py riskrunway://export?job_id=123&server=https://app.riskrunway.com")
        sys.exit(1)
    
    url = sys.argv[1]
    logger.info(f"Received URL: {url}")
    
    # Parse the URL
    try:
        params = parse_riskrunway_url(url)
    except Exception as e:
        logger.error(f"Failed to parse URL: {e}")
        print(f"❌ Error: Invalid URL format")
        sys.exit(1)
    
    action = params.get('action')
    logger.info(f"Action: {action}, Params: {params}")
    
    # Handle different actions
    if action == 'export':
        success = handle_export(params)
    else:
        logger.error(f"Unknown action: {action}")
        print(f"❌ Error: Unknown action '{action}'")
        success = False
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
