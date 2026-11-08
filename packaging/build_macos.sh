#!/bin/bash
# build_macos.sh — build "Wax Digger.app" and package it into WaxDigger.dmg.
# Run on macOS with: pip install pyinstaller pillow flask pywebview
set -e

cd "$(dirname "$0")/.."   # repo root
APP="Wax Digger"

python3 packaging/fetch_binaries.py
python3 packaging/make_icons.py
mkdir -p packaging/bin
(cd vendor/gofi && go build -o ../../packaging/bin/wax-gofi ./cmd/wax-gofi)

# Keep separately archived releases in dist when producing a fresh build.
rm -rf build "dist/$APP" "dist/$APP.app" "dist/dmg" "dist/WaxDigger.dmg"

# bundle whatever binaries fetch_binaries produced (yt-dlp, ffmpeg, maybe ffprobe)
BIN_ARGS=()
for f in packaging/bin/*; do
  BIN_ARGS+=(--add-binary "$f:bin")
done

pyinstaller --noconfirm --windowed --name "$APP" \
  --icon packaging/icon.icns \
  --osx-bundle-identifier io.github.sukaslitno.waxdigger \
  --add-data "ytdlp-gui/templates:templates" \
  --add-data "packaging/app_icon.png:." \
  --add-data "vendor/gofi/LICENSE:licenses/gofi" \
  --collect-all webview \
  "${BIN_ARGS[@]}" \
  ytdlp-gui/app.py

# DMG with an Applications drag-target
STAGE="dist/dmg"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "dist/$APP.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "$APP" -srcfolder "$STAGE" -ov -format UDZO "dist/WaxDigger.dmg"

echo "✓ dist/WaxDigger.dmg"
