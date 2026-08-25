#!/bin/zsh
# Builds SakethWiki.app -- a native window around the local dashboard -- and
# installs it to /Applications.
#
# The bundle contains no application logic. It exists so the dashboard has a
# Dock icon, an app identity and a window of its own. launch.sh remains the
# owner of the backend/frontend processes. Mirrors
# ~/App Job Tracker/scripts/build_macos_app.sh.
#
# Usage: ./build_macos_app.sh [--no-install]

set -eu

ROOT_DIR="${0:A:h}"
cd "$ROOT_DIR"

APP_NAME="SakethWiki"
BUNDLE_ID="com.sakethv7.sakethwiki"
BUILD_DIR="$ROOT_DIR/build"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
INSTALL_DIR="/Applications/$APP_NAME.app"

echo "==> Preparing bundle skeleton"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

echo "==> Using existing icon"
cp "$BUILD_DIR/SakethWiki.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"

echo "==> Compiling"
swiftc -O \
  -target arm64-apple-macos12.0 \
  -o "$BUILD_DIR/sakethwiki-arm64" \
  macapp/main.swift
if swiftc -target x86_64-apple-macos12.0 -O -o "$BUILD_DIR/sakethwiki-x86_64" macapp/main.swift 2>/dev/null; then
  lipo -create -output "$APP_DIR/Contents/MacOS/SakethWiki" \
    "$BUILD_DIR/sakethwiki-arm64" "$BUILD_DIR/sakethwiki-x86_64"
else
  echo "    (x86_64 slice unavailable; building arm64 only)"
  cp "$BUILD_DIR/sakethwiki-arm64" "$APP_DIR/Contents/MacOS/SakethWiki"
fi
chmod +x "$APP_DIR/Contents/MacOS/SakethWiki"

echo "==> Writing Info.plist"
# SWProjectRoot lets the installed bundle find launch.sh so a Dock click
# still works when the backend/frontend are not already running.
cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>SakethWiki</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>SWProjectRoot</key><string>$ROOT_DIR</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

echo "==> Signing"
codesign --force --deep --sign - "$APP_DIR" 2>/dev/null && echo "    signed ad-hoc" || echo "    (signing skipped)"

if [ "${1:-}" = "--no-install" ]; then
  echo "==> Built at $APP_DIR (not installed)"
  exit 0
fi

echo "==> Installing to $INSTALL_DIR"
osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
sleep 1
rm -rf "$INSTALL_DIR"
cp -R "$APP_DIR" "$INSTALL_DIR"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$INSTALL_DIR" >/dev/null 2>&1 || true

echo "==> Done. $INSTALL_DIR"
