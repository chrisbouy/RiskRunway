#!/bin/bash
# Install RiskRunwayLauncher.app and register riskrunway:// protocol handler

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="RiskRunwayLauncher"
APP_BUNDLE="$SCRIPT_DIR/build/$APP_NAME.app"
INSTALL_DIR="/Applications"

echo "Installing RiskRunway Launcher for macOS..."
echo ""

# Check if app bundle exists
if [ ! -d "$APP_BUNDLE" ]; then
    echo "⚠️  App bundle not found. Building first..."
    "$SCRIPT_DIR/build_macos.sh"
fi

# Check if already installed
if [ -d "$INSTALL_DIR/$APP_NAME.app" ]; then
    echo "⚠️  $APP_NAME.app already exists in /Applications"
    read -p "   Replace it? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 0
    fi
    rm -rf "$INSTALL_DIR/$APP_NAME.app"
fi

# Copy to Applications
echo "→ Copying $APP_NAME.app to /Applications..."
cp -R "$APP_BUNDLE" "$INSTALL_DIR/"

# Register protocol handler (LSRegisterURL)
echo "→ Registering riskrunway:// protocol handler..."
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "$INSTALL_DIR/$APP_NAME.app"

echo ""
echo "✓ Installation complete!"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Permission Setup
# ─────────────────────────────────────────────────────────────────────────────

echo "→ Setting up required macOS permissions..."
echo ""
echo "  RiskRunway needs two permissions for Terminal to fill AMS forms:"
echo "    1. Accessibility — to click and type into fields"
echo "    2. Screen Recording — to see the AMS form fields"
echo ""

# Open Accessibility settings
osascript -e 'display dialog "Step 1 of 2:\n\nSystem Settings will open to Accessibility.\nFind \"Terminal\" in the list and toggle it ON.\n\nClicking Next will open settings" buttons {"Next"} default button "Next" with title "RiskRunway Setup (1/2)" with icon note'
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

osascript -e 'display dialog "Enable Terminal in the Accessibility list, then click Next." buttons {"Next"} default button "Next" with title "RiskRunway Setup (1/2)" with icon note'

# Open Screen Recording settings
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"

osascript -e 'display dialog "Step 2 of 2:\n\nEnable Terminal in the Screen Recording list, then click Done." buttons {"Done"} default button "Done" with title "RiskRunway Setup (2/2)" with icon note'

# Mark permissions as granted so the launcher doesn't ask again
mkdir -p "$HOME/.riskrunway"
touch "$HOME/.riskrunway/.permissions_granted"

echo "✓ Permissions configured!"
echo ""
echo "You can now use RiskRunway Export from your browser."
echo ""
echo "Test it:"
echo "  open 'riskrunway://export?job_id=123&server=https://example.com'"
echo ""
echo "To uninstall:"
echo "  rm -rf /Applications/$APP_NAME.app"
echo "  rm -rf ~/.riskrunway"
echo ""
