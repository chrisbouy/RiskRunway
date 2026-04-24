# RiskRunway Launcher

Browser protocol handler for RiskRunway AMS Export. Enables seamless one-click export from the web app to the local desktop agent.

## How It Works

```
User clicks "Export to AMS" in browser
        ↓
Browser opens: riskrunway://export?job_id=123&server=https://app.riskrunway.com
        ↓
RiskRunwayLauncher spawns local_agent.py --job-id 123 --server https://...
        ↓
local_agent fetches job, shows overlay, user positions over AMS, clicks button
        ↓
Screenshot → Claude Vision → Auto-fill → Job complete → local_agent exits
```

**No background process. No polling. On-demand only.**

## Installation

### macOS

1. Build the app bundle:
   ```bash
   cd launcher
   ./build_macos.sh
   ```

2. Install to Applications:
   ```bash
   ./install_macos.sh
   ```

Or drag `build/RiskRunwayLauncher.app` to `/Applications` manually.

### Windows

1. Build the launcher:
   ```cmd
   cd launcher
   build_windows.bat
   ```

2. Copy `build/RiskRunwayLauncher` folder to `C:\Program Files\RiskRunway\`

3. Run `install.bat` as Administrator

## Architecture

| Component | Purpose |
|-----------|---------|
| `launcher.py` | Parses `riskrunway://` URLs, spawns local_agent |
| `RiskRunwayLauncher.app` (macOS) | App bundle registered as protocol handler |
| `RiskRunwayLauncher.bat` (Windows) | Batch script registered in registry |
| `local_agent.py --job-id N` | Single-shot execution mode (no polling) |

## Testing

### macOS
```bash
open 'riskrunway://export?job_id=123&server=https://example.com'
```

### Windows
```cmd
start riskrunway://export?job_id=123&server=https://example.com
```

## Troubleshooting

**"Could not find local_agent.py"**
- Ensure `local_agent.py` is in the same directory as the launcher
- Or in the parent project directory

**Protocol not working after install**
- macOS: Run `/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f /Applications/RiskRunwayLauncher.app`
- Windows: Ensure you ran `install.bat` as Administrator

**Job not found error**
- Job must be in `pending` status
- Check that the server URL matches your RiskRunway instance
