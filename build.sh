#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
destination="$PWD/build/CodexMenu.app"
stage=$(mktemp -d /private/tmp/codex-menu-build.XXXXXX)
trap 'rm -rf "$stage"' EXIT
app="$stage/CodexMenu.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$PWD/build/module-cache"
/usr/bin/swiftc -O -module-cache-path "$PWD/build/module-cache" main.swift -o "$app/Contents/MacOS/CodexMenu"
cp -X backend.py "$app/Contents/Resources/backend.py"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.codex-menu</string>
<key>CFBundleName</key><string>CodexMenu</string>
<key>CFBundleDisplayName</key><string>Codex Menu</string>
<key>CFBundleExecutable</key><string>CodexMenu</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
<key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$app/Contents/Info.plist"
# Only clean generated build products; source files and installed apps are untouched.
/usr/bin/xattr -cr "$app"
/usr/bin/codesign --force --sign - "$app"
/usr/bin/codesign --verify --deep --strict "$app"
if [ -e "$destination" ]; then
  # This directory contains only this script's generated product.
  mv "$destination" "$stage/previous.app"
fi
mv "$app" "$destination"
/usr/bin/codesign --verify --deep --strict "$destination"
printf 'Built: %s\n' "$destination"
