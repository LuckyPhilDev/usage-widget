#!/bin/sh
# Builds dist/UsageWidget.app and dist/UsageWidget-mac.zip for this Mac's architecture.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements-mac.txt pyinstaller==6.22.3
rm -rf build dist
.venv/bin/python -m PyInstaller --windowed --noconfirm --name UsageWidget \
    --osx-bundle-identifier com.luckyphil.usagewidget usage_mac.py
# Menu bar only, no Dock icon. Editing Info.plist breaks PyInstaller's signature, so re-sign ad hoc.
plutil -replace LSUIElement -bool true dist/UsageWidget.app/Contents/Info.plist
codesign --force --deep --sign - dist/UsageWidget.app
ditto -c -k --keepParent dist/UsageWidget.app dist/UsageWidget-mac.zip
echo "Built for $(uname -m)"
shasum -a 256 dist/UsageWidget-mac.zip
