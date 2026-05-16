#!/bin/bash
# Upload AMS Agent setup files to S3 for user download via the in-app wizard.
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - S3 bucket created and accessible
#
# Usage:
#   ./upload_to_s3.sh [bucket-name]
#
# Uploads:
#   - RiskRunwayLauncher.app.zip (macOS)         → s3://bucket/agent-setup/RiskRunwayLauncher.app.zip
#   - RiskRunway-Windows-Setup.zip (Windows)     → s3://bucket/agent-setup/RiskRunway-Windows-Setup.zip

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
DIST_DIR="$SCRIPT_DIR/dist"

# Use argument or fall back to env var
BUCKET="${1:-$AMS_AGENT_S3_BUCKET}"
PREFIX="${AMS_AGENT_S3_PREFIX:-agent-setup/}"

if [ -z "$BUCKET" ]; then
    echo "❌ No S3 bucket specified."
    echo "   Usage: ./upload_to_s3.sh <bucket-name>"
    echo "   Or set AMS_AGENT_S3_BUCKET environment variable."
    exit 1
fi

echo "Uploading AMS Agent setup files to s3://$BUCKET/$PREFIX"
echo ""

# ─── macOS: zip the .app bundle and upload ───────────────────────────────────
APP_BUNDLE="$BUILD_DIR/RiskRunwayLauncher.app"
MACOS_ZIP="$DIST_DIR/RiskRunwayLauncher.app.zip"

if [ -d "$APP_BUNDLE" ]; then
    mkdir -p "$DIST_DIR"
    echo "→ Zipping $APP_BUNDLE..."
    (cd "$BUILD_DIR" && zip -r "$MACOS_ZIP" "RiskRunwayLauncher.app" -x "*.DS_Store")
    echo "→ Uploading macOS installer..."
    aws s3 cp "$MACOS_ZIP" "s3://$BUCKET/${PREFIX}RiskRunwayLauncher.app.zip" \
        --content-type "application/zip"
    echo "✓ macOS: s3://$BUCKET/${PREFIX}RiskRunwayLauncher.app.zip"
else
    echo "⚠️  macOS .app bundle not found at $APP_BUNDLE"
    echo "   Run ./build_macos.sh first."
fi

echo ""

# ─── Windows: upload the existing zip ────────────────────────────────────────
WIN_ZIP="$DIST_DIR/RiskRunway-Windows-Setup.zip"

if [ -f "$WIN_ZIP" ]; then
    echo "→ Uploading Windows setup zip..."
    aws s3 cp "$WIN_ZIP" "s3://$BUCKET/${PREFIX}RiskRunway-Windows-Setup.zip" \
        --content-type "application/zip"
    echo "✓ Windows: s3://$BUCKET/${PREFIX}RiskRunway-Windows-Setup.zip"
else
    echo "⚠️  Windows zip not found at $WIN_ZIP (skipping)"
    echo "   Expected: launcher/dist/RiskRunway-Windows-Setup.zip"
fi

echo ""
echo "✓ Done! Files are now available for download via the in-app wizard."
echo ""
echo "Verify with:"
echo "  aws s3 ls s3://$BUCKET/$PREFIX"
echo ""
