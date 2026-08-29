#!/usr/bin/env bash
# Build Saturday Linux packages from the PyInstaller output.
#   python -m PyInstaller saturday.spec --noconfirm
#   bash scripts/build_linux.sh [deb|rpm|appimage|all]
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-all}"
VERSION="$(python -c 'from saturday import __version__; print(__version__)')"
ARCH="$(uname -m)"
DIST="dist/Saturday"
STAGE="build/linux"
PKGROOT="$STAGE/saturday-$VERSION-$ARCH"

[ -x "$DIST/Saturday" ] || { echo "run PyInstaller first (dist/Saturday missing)"; exit 1; }

build_tree() {
  rm -rf "$PKGROOT"
  install -d "$PKGROOT/usr/lib/saturday" "$PKGROOT/usr/bin" \
            "$PKGROOT/usr/share/applications" \
            "$PKGROOT/usr/share/icons/hicolor/scalable/apps" \
            "$PKGROOT/usr/share/icons/hicolor/256x256/apps" \
            "$PKGROOT/DEBIAN"
  cp -r "$DIST/." "$PKGROOT/usr/lib/saturday/"
  install -m 0755 packaging/linux/saturday-wrapper.sh "$PKGROOT/usr/bin/saturday"
  install -m 0644 packaging/linux/saturday.desktop "$PKGROOT/usr/share/applications/saturday.desktop"
  install -m 0644 src/saturday/webui_assets/favicon.svg "$PKGROOT/usr/share/icons/hicolor/scalable/apps/saturday.svg"
  install -m 0644 packaging/icons/saturday-256.png "$PKGROOT/usr/share/icons/hicolor/256x256/apps/saturday.png"
  cat > "$PKGROOT/DEBIAN/control" <<EOF
Package: saturday
Version: $VERSION
Section: utils
Priority: optional
Architecture: $(dpkg --print-architecture 2>/dev/null || echo amd64)
Installed-Size: $(du -sk "$PKGROOT/usr" | cut -f1)
Maintainer: Saturday Labs <saturday@localhost>
Description: The auditable minimal agentic AI harness
 Local-first agent desktop app: streaming chat, 25+ tools, MCP,
 background computer use, sessions with checkpoints. Bring your own
 provider key (DeepSeek/OpenAI/OpenRouter/Anthropic/Gemini/Ollama/...).
Depends: libc6
Suggests: chromium | google-chrome | microsoft-edge-stable
EOF
}

if [ "$TARGET" = deb ] || [ "$TARGET" = all ]; then
  build_tree
  dpkg-deb --build --root-owner-group "$PKGROOT" "build/linux/saturday_${VERSION}_${ARCH}.deb"
  echo "deb  -> build/linux/saturday_${VERSION}_${ARCH}.deb"
fi

if [ "$TARGET" = rpm ] || [ "$TARGET" = all ]; then
  if command -v rpmbuild >/dev/null 2>&1; then
    build_tree
    RPMTOP="$STAGE/rpmbuild"
    rm -rf "$RPMTOP"; mkdir -p "$RPMTOP/BUILD" "$RPMTOP/RPMS" "$RPMTOP/SOURCES" "$RPMTOP/SPECS" "$RPMTOP/SRPMS"
    cat > "$RPMTOP/SPECS/saturday.spec" <<EOF
Name:           saturday
Version:        $VERSION
Release:        1%{?dist}
Summary:        The auditable minimal agentic AI harness
License:        MIT
BuildArch:      x86_64
AutoReqProv:    no
%description
Local-first agent desktop app: streaming chat, tools, MCP, background
computer use. Bring your own provider key.
%install
cp -r "$PKGROOT/usr" "%{buildroot}/usr"
%files
/usr/bin/saturday
/usr/lib/saturday
/usr/share/applications/saturday.desktop
/usr/share/icons/hicolor/scalable/apps/saturday.svg
/usr/share/icons/hicolor/256x256/apps/saturday.png
EOF
    rpmbuild -bb --define "_topdir $RPMTOP" "$RPMTOP/SPECS/saturday.spec" >/dev/null
    find "$RPMTOP/RPMS" -name '*.rpm' -exec mv {} "$STAGE/" \;
    echo "rpm  -> $STAGE/*.rpm"
  else
    echo "rpm  skipped (rpmbuild not installed; apt install rpm)"
  fi
fi

if [ "$TARGET" = appimage ] || [ "$TARGET" = all ]; then
  APPDIR="$STAGE/AppDir"
  rm -rf "$APPDIR"
  build_tree
  # flatten usr tree to AppDir layout expected by linuxdeploy
  mkdir -p "$APPDIR"
  cp -r "$PKGROOT/usr/." "$APPDIR/"
  mv "$APPDIR/lib/saturday" "$APPDIR/lib_saturday_tmp" 2>/dev/null || true
  mv "$APPDIR/lib_saturday_tmp" "$APPDIR/lib/saturday" 2>/dev/null || true
  install -m 0755 packaging/linux/AppRun "$APPDIR/AppRun"
  install -m 0644 packaging/linux/saturday.desktop "$APPDIR/saturday.desktop"
  install -m 0644 packaging/icons/saturday-256.png "$APPDIR/saturday.png"
  LD="$STAGE/linuxdeploy-$ARCH.AppImage"
  [ -f "$LD" ] || curl -fL "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-$ARCH.AppImage" -o "$LD"
  chmod +x "$LD"
  cd "$STAGE"
  APPIMAGE_EXTRACT_AND_RUN=1 NO_STRIP=1 "$LD" --appdir AppDir --output appimage
  mv "Saturday-$VERSION-$ARCH.AppImage" saturday_"${VERSION}"_"${ARCH}".AppImage 2>/dev/null || true
  cd - >/dev/null
  echo "appimage -> $STAGE/*.AppImage"
fi
