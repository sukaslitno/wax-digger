#!/bin/bash
# make-safari.sh — convert the built Chrome package into a Safari Web Extension
# Xcode project. macOS + Xcode (Command Line Tools) required.
#
#   ./scripts/make-safari.sh
#
# Then open the generated Xcode project, set your signing team, and Run. The
# extension appears in Safari → Settings → Extensions (enable "Allow unsigned"
# in Develop menu for local builds).
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

if ! command -v xcrun >/dev/null 2>&1; then
  echo "xcrun not found — install Xcode / Command Line Tools first." >&2
  exit 1
fi

# make sure we have a fresh Chrome-format build to convert
node build.js

rm -rf dist/safari
xcrun safari-web-extension-converter dist/chrome \
  --project-location dist/safari \
  --app-name "Wax Digger" \
  --bundle-identifier "hr.greenlight.waxdigger" \
  --no-open --force --macos-only

echo
echo "✓ Safari project created in dist/safari/"
echo "  Open the .xcodeproj, pick a signing team, and press Run."
