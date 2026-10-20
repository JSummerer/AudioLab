#!/bin/bash
# Maintainer build: produce a self-contained core app and a drag-to-Applications disk image.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${AUDIOLAB_PACKAGE_WORK_DIR:-$ROOT/.work/macos-package}"
PYTHON="${AUDIOLAB_BUILD_PYTHON:-python3}"
[[ "$(uname -s)" == Darwin ]] || { echo "Build the macOS app on macOS." >&2; exit 1; }
command -v uv >/dev/null || { echo "Install uv before building (see packaging/macos/README.md)." >&2; exit 1; }
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
export UV_CACHE_DIR="$WORK/uv-cache"
export PYINSTALLER_CONFIG_DIR="$WORK/pyinstaller-cache"
export MACOSX_DEPLOYMENT_TARGET=14.0
uv venv --python "$PYTHON" --allow-existing "$WORK/venv"
BUILD_PYTHON="$WORK/venv/bin/python"
"$BUILD_PYTHON" - <<'PY'
import sys
from pathlib import Path
assert sys.version_info[:3] == (3, 12, 15), 'Use the pinned Python runtime from the macOS build workflow.'
build = Path(sys.base_prefix) / 'BUILD'
assert build.is_file() and build.read_text().strip() == '20261001', \
    'Use Python Build Standalone 20261001 to match the reviewed runtime notices.'
PY
uv pip sync --python "$BUILD_PYTHON" "$ROOT/packaging/macos/requirements.lock"
uv pip install --python "$BUILD_PYTHON" --no-deps -e "$ROOT"
export AUDIOLAB_BUILD_PYTHON="$BUILD_PYTHON"
export AUDIOLAB_MEDIA_BUILD_DIR="$WORK/media"
# Obtain all pinned inputs before spending time compiling the media engine.
"$BUILD_PYTHON" "$ROOT/packaging/macos/third_party.py" fetch --directory "$WORK/media/downloads"
bash "$ROOT/packaging/macos/build_ffmpeg.sh"
export AUDIOLAB_PACKAGE_FFMPEG="$WORK/media/install/bin/ffmpeg"

# Exact notices/source companion are assembled before freezing, not downloaded on the user's Mac.
export AUDIOLAB_PACKAGE_NOTICES="$WORK/ThirdParty"
"$BUILD_PYTHON" "$ROOT/packaging/macos/third_party.py" native-notices \
  --groups media,soundfile,certifi,scipy-runtime,deno-roots \
  --directory "$WORK/media/downloads" --output "$WORK/native-notices"
PACKAGES="$("$BUILD_PYTHON" "$ROOT/packaging/macos/bundle_inventory.py" --distributions)"
"$BUILD_PYTHON" "$ROOT/packaging/macos/third_party.py" notices --packages "$PACKAGES" \
  --output "$AUDIOLAB_PACKAGE_NOTICES" \
  --extra "$ROOT/packaging/macos/notices/proxy_tools-0.1.0" \
  --extra "$ROOT/packaging/macos/notices/pyobjc-core-12.2.2" \
  --extra "$ROOT/packaging/macos/notices/pyobjc-framework-UniformTypeIdentifiers-12.2.2" \
  --extra "$ROOT/packaging/macos/notices/deno-2.9.7"
"$BUILD_PYTHON" "$ROOT/packaging/macos/bundle_inventory.py" --prepare-notices "$WORK"

cd "$ROOT"
"$BUILD_PYTHON" -m PyInstaller --noconfirm --workpath "$WORK/pyinstaller-work" \
  --distpath "$WORK/dist" packaging/macos/AudioLab.spec
APP="$WORK/dist/AudioLab.app"
codesign --verify --deep --strict "$APP"
"$BUILD_PYTHON" "$ROOT/packaging/macos/bundle_inventory.py" --verify-app "$APP" | tee "$WORK/bundle-inventory.json"

VERSION="$("$BUILD_PYTHON" -c 'from audiolab import __version__; print(__version__)')"
ARCH="$(uname -m)"
RELEASE="$WORK/release-$VERSION-$ARCH"
mkdir "$RELEASE"  # fail rather than silently replace a prepared release
"$BUILD_PYTHON" "$ROOT/packaging/macos/smoke.py" "$APP" "$RELEASE/smoke"
STAGE="$WORK/dmg-stage"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/AudioLab.app"
ln -sfn /Applications "$STAGE/Applications"
cp "$ROOT/packaging/macos/Install and uninstall.txt" "$STAGE/Read me first.txt"
hdiutil create -ov -format UDZO -volname AudioLab -srcfolder "$STAGE" \
  "$RELEASE/AudioLab-$VERSION-macOS-$ARCH.dmg"
"$BUILD_PYTHON" "$ROOT/packaging/macos/third_party.py" companion \
  --directory "$WORK/media/downloads" \
  --output "$RELEASE/AudioLab-$VERSION-third-party-sources-$ARCH.tar.gz"
(
  cd "$RELEASE"
  shasum -a 256 ./*.dmg ./*.tar.gz > SHA256SUMS.txt
)
echo "Built $RELEASE"
