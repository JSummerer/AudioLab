#!/bin/bash
# Maintainer build only. The user's Mac receives the complete frozen archive.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${AUDIOLAB_AI_WORK_DIR:-$ROOT/.work/ai-package}"
PYTHON="${AUDIOLAB_BUILD_PYTHON:-python3}"
VERSION="${AUDIOLAB_AI_VERSION:?Set AUDIOLAB_AI_VERSION to an intentional new helper version.}"
RELEASE_TAG="${AUDIOLAB_AI_RELEASE_TAG:-ai-v$VERSION}"
PUBLISHED_VERSION="$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["catalog"]["version"])' \
  "$ROOT/packaging/ai/published-helper.json")"
[[ "$VERSION" != "$PUBLISHED_VERSION" ]] || {
  echo "AI $VERSION is already published and immutable. Reuse it or choose a new helper version." >&2; exit 1;
}
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'Build the AI pack on Apple silicon macOS.' >&2; exit 1; }
command -v uv >/dev/null || { echo 'Install uv before building (see packaging/ai/README.md).' >&2; exit 1; }
mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
export AUDIOLAB_AI_WORK_DIR="$WORK"
export UV_CACHE_DIR="$WORK/uv-cache"
export PYINSTALLER_CONFIG_DIR="$WORK/pyinstaller-cache"
export MACOSX_DEPLOYMENT_TARGET=14.0
uv venv --python "$PYTHON" --allow-existing "$WORK/venv"
BUILD_PYTHON="$WORK/venv/bin/python"
"$BUILD_PYTHON" - <<'PY'
import sys
from pathlib import Path
assert sys.version_info[:3] == (3, 12, 15), 'Use the pinned Python Build Standalone runtime.'
assert (Path(sys.base_prefix) / 'BUILD').read_text().strip() == '20261001', 'Use Python Build Standalone 20261001.'
PY
uv pip sync --python "$BUILD_PYTHON" "$ROOT/packaging/ai/requirements.lock"
"$BUILD_PYTHON" "$ROOT/packaging/ai/prepare.py" --work "$WORK" --version "$VERSION"
cd "$WORK"
"$BUILD_PYTHON" -m PyInstaller --noconfirm --workpath "$WORK/pyinstaller-work" \
  --distpath "$WORK/dist" "$ROOT/packaging/ai/AudioLabAI.spec"
"$BUILD_PYTHON" "$ROOT/packaging/ai/release.py" --work "$WORK" --version "$VERSION" --release-tag "$RELEASE_TAG"
"$BUILD_PYTHON" "$ROOT/packaging/ai/smoke.py" \
  "$WORK/release/AudioLab-AI-$VERSION-macOS-arm64.tar.gz" "$WORK/smoke"
