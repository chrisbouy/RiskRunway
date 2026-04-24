#!/bin/bash
# Build RiskRunwayLauncher.app for macOS
# Creates a proper .app bundle that can be registered as a protocol handler

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/build"
APP_NAME="RiskRunwayLauncher"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"

echo "Building $APP_NAME.app for macOS..."

# Clean previous build
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources"

# Create Info.plist
cat > "$APP_BUNDLE/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>RiskRunwayLauncher</string>
    <key>CFBundleIdentifier</key>
    <string>com.riskrunway.launcher</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>RiskRunwayLauncher</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.12</string>
    <key>LSUIElement</key>
    <true/>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>RiskRunway Protocol</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>riskrunway</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
EOF

# Copy the Python launcher script
if [ -f "$SCRIPT_DIR/RiskRunwayLauncher.py" ]; then
    cp "$SCRIPT_DIR/RiskRunwayLauncher.py" "$APP_BUNDLE/Contents/MacOS/RiskRunwayLauncher"
    chmod +x "$APP_BUNDLE/Contents/MacOS/RiskRunwayLauncher"
    echo "✓ Copied RiskRunwayLauncher.py"
else
    echo "✗ RiskRunwayLauncher.py not found in $SCRIPT_DIR"
    exit 1
fi

# Copy local_agent.py and launcher.py to Resources if they exist
if [ -f "$PROJECT_ROOT/local_agent.py" ]; then
    cp "$PROJECT_ROOT/local_agent.py" "$APP_BUNDLE/Contents/Resources/"
    echo "✓ Bundled local_agent.py"
fi

if [ -f "$SCRIPT_DIR/launcher.py" ]; then
    cp "$SCRIPT_DIR/launcher.py" "$APP_BUNDLE/Contents/Resources/"
    echo "✓ Bundled launcher.py"
fi

# Create symlink in project root for easy access
ln -sf "$APP_BUNDLE" "$PROJECT_ROOT/RiskRunwayLauncher.app"

echo ""
echo "✓ Built $APP_NAME.app"
echo "  Location: $APP_BUNDLE"
echo ""
echo "Next steps:"
echo "  1. Run: ./install_macos.sh"
echo "  2. Or drag $APP_NAME.app to /Applications"
echo ""
