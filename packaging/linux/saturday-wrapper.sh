#!/bin/sh
# Saturday deb/rpm/AppImage launcher wrapper.
# System install: execs the bundled onedir binary; AppImage layout also works
# because $APPDIR/bin/saturday is this same wrapper with HERE pointing inside.
HERE="$(dirname "$(readlink -f "$0")")"
if [ -x "$HERE/usr/lib/saturday/Saturday" ]; then
  exec "$HERE/usr/lib/saturday/Saturday" "$@"
fi
if [ -n "$APPDIR" ] && [ -x "$APPDIR/usr/lib/saturday/Saturday" ]; then
  exec "$APPDIR/usr/lib/saturday/Saturday" "$@"
fi
exec "$HERE/../lib/saturday/Saturday" "$@"
