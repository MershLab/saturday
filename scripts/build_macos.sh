#!/usr/bin/env bash
# Build the Saturday macOS app bundle + DMG.
#   python -m PyInstaller saturday.spec --noconfirm
#   bash scripts/build_macos.sh
# Produces dist/Saturday-macos-<arch>.dmg (arm64 on Apple Silicon runners,
# x86_64 on macos-13). Ad-hoc codesigns so arm64 executes at all; proper
# Developer-ID signing/notarization needs certs and is intentionally out of scope.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(python -c 'from saturday import __version__; print(__version__)')"
ARCH="$(uname -m)"
APP="dist/Saturday.app"

[ -d "$APP" ] || { echo "Saturday.app missing — run PyInstaller first"; exit 1; }

# ad-hoc signature (required on arm64; harmless on x86_64)
codesign --force --deep -s - "$APP" 2>/dev/null || echo "codesign skipped"

DMG="dist/Saturday-macos-${ARCH}.dmg"
rm -f "$DMG"
hdiutil create -volname "Saturday" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null
echo "dmg -> $DMG (v$VERSION, $ARCH)"
