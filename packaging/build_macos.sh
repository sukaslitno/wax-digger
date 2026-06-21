#!/bin/bash
# build_macos.sh — build "Wax Digger.app" and package it into WaxDigger.dmg.
# Run on macOS with: pip install pyinstaller pillow
set -e

cd "$(dirname "$0")/.."   # repo root
APP="Wax Digger"

python3 packaging/fetch_binaries.py
python3 packaging/make_icons.py

rm -rf build dist

# bundle whatever binaries fetch_binaries produced (yt-dlp, ffmpeg, maybe ffprobe)
BIN_ARGS=()
for f in packaging/bin/*; do
  BIN_ARGS+=(--add-binary "$f:bin")
done

pyinstaller --noconfirm --windowed --name "$APP" \
  --icon packaging/icon.icns \
  --osx-bundle-identifier hr.greenlight.waxdigger \
  --add-data "ytdlp-gui/templates:templates" \
  "${BIN_ARGS[@]}" \
  ytdlp-gui/app.py

# DMG with an Applications drag-target
STAGE="dist/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "dist/$APP.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "dist/WaxDigger.dmg"

echo "✓ dist/WaxDigger.dmg"
