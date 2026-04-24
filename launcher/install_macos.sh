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
echo "You can now use RiskRunway Export from your browser."
echo ""
echo "Test it:"
echo "  open 'riskrunway://export?job_id=123&server=https://example.com'"
echo ""
echo "To uninstall:"
echo "  rm -rf /Applications/$APP_NAME.app"
echo ""
