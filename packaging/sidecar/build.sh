#!/usr/bin/env bash
# Build the headroom-runtime frozen sidecar (PyInstaller onedir, macOS arm64).
#
# Usage: ./build.sh
# Output: dist/headroom-runtime/               (onedir bundle)
#         dist/headroom-runtime-v<ver>-aarch64-apple-darwin.tar.gz
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
if [[ ! -x "$VENV/bin/pyinstaller" ]]; then
    echo "error: $VENV/bin/pyinstaller not found (create the venv and pip install 'headroom-ai[proxy]' pyinstaller)" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
if [[ -z "$VERSION" ]]; then
    echo "error: VERSION file is empty" >&2
    exit 1
fi

echo "==> Building headroom-runtime v$VERSION (onedir)"
"$VENV/bin/pyinstaller" --clean --noconfirm headroom-runtime.spec

ONEDIR="$HERE/dist/headroom-runtime"
if [[ ! -x "$ONEDIR/headroom-runtime" ]]; then
    echo "error: expected executable at $ONEDIR/headroom-runtime" >&2
    exit 1
fi

echo "==> Onedir size:"
du -sh "$ONEDIR"

TARBALL="dist/headroom-runtime-v${VERSION}-aarch64-apple-darwin.tar.gz"
echo "==> Creating $TARBALL"
tar -C "$HERE/dist" -czf "$HERE/$TARBALL" headroom-runtime
du -sh "$HERE/$TARBALL"

echo "==> Done"
