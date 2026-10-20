#!/bin/bash
# Build only inside the disposable media-build directory; no Homebrew libraries
# or tools are copied into the resulting ffmpeg / ffprobe executables.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${AUDIOLAB_MEDIA_BUILD_DIR:-$REPO_DIR/.work/macos-package/media}"
PYTHON="${AUDIOLAB_BUILD_PYTHON:-$REPO_DIR/.venv/bin/python}"
JOBS="${AUDIOLAB_BUILD_JOBS:-4}"
case "$JOBS" in 1|2|3|4) ;; *) echo 'AUDIOLAB_BUILD_JOBS must be 1–4.' >&2; exit 2 ;; esac
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo 'This recipe targets macOS arm64 on an Apple Silicon build host.' >&2
    exit 2
fi

mkdir -p "$BUILD_DIR"
BUILD_DIR="$(cd "$BUILD_DIR" && pwd)"
PREFIX="$BUILD_DIR/install"
SOURCE_DIR="$BUILD_DIR/source"
DOWNLOAD_DIR="$BUILD_DIR/downloads"
mkdir -p "$PREFIX" "$SOURCE_DIR"
"$PYTHON" "$SCRIPT_DIR/third_party.py" fetch --groups media,build-tool --directory "$DOWNLOAD_DIR"

# A clean pkg-config search prevents accidental linkage to the operator's tools.
export MACOSX_DEPLOYMENT_TARGET=14.0
export SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"
export CC="$(xcrun --find clang)"
export CXX="$(xcrun --find clang++)"
export CFLAGS='-O2 -arch arm64 -mmacosx-version-min=14.0'
export CXXFLAGS="$CFLAGS"
export LDFLAGS='-arch arm64 -mmacosx-version-min=14.0'
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_PATH=''
export PATH="$PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin"

extract() {
    local archive="$1" name="$2"
    if [[ ! -d "$SOURCE_DIR/$name" ]]; then
        /usr/bin/tar -xf "$DOWNLOAD_DIR/$archive" -C "$SOURCE_DIR"
    fi
}

extract pkgconf-2.5.1.tar.xz pkgconf-2.5.1
cd "$SOURCE_DIR/pkgconf-2.5.1"
./configure --prefix="$PREFIX" --disable-shared --enable-static
make -j "$JOBS"
make install
export PKG_CONFIG="$PREFIX/bin/pkgconf"

extract lame-3.100.tar.gz lame-3.100
cd "$SOURCE_DIR/lame-3.100"
./configure --prefix="$PREFIX" --host=aarch64-apple-darwin --disable-shared --enable-static --disable-frontend
make -j "$JOBS"
make install

extract opus-1.5.2.tar.gz opus-1.5.2
cd "$SOURCE_DIR/opus-1.5.2"
./configure --prefix="$PREFIX" --host=aarch64-apple-darwin --disable-shared --enable-static \
    --disable-doc --disable-extra-programs
make -j "$JOBS"
make install

extract ffmpeg-7.1.tar.xz ffmpeg-7.1
cd "$SOURCE_DIR/ffmpeg-7.1"
./configure --prefix="$PREFIX" --arch=arm64 --target-os=darwin --cc="$CC" --cxx="$CXX" \
    --disable-autodetect --disable-gpl --disable-nonfree --disable-shared --enable-static \
    --disable-ffplay --disable-debug --disable-doc --enable-libmp3lame --enable-libopus \
    --enable-securetransport --enable-zlib --pkg-config="$PKG_CONFIG" --pkg-config-flags=--static \
    --extra-cflags="-I$PREFIX/include $CFLAGS" --extra-ldflags="-L$PREFIX/lib $LDFLAGS"
make -j "$JOBS"
make install

mkdir -p "$BUILD_DIR/evidence"
"$PREFIX/bin/ffmpeg" -version > "$BUILD_DIR/evidence/ffmpeg-version.txt" 2>&1
"$PREFIX/bin/ffmpeg" -L > "$BUILD_DIR/evidence/ffmpeg-license.txt" 2>&1
"$PREFIX/bin/ffmpeg" -encoders > "$BUILD_DIR/evidence/ffmpeg-encoders.txt" 2>&1
"$PREFIX/bin/ffmpeg" -filters > "$BUILD_DIR/evidence/ffmpeg-filters.txt" 2>&1
"$PREFIX/bin/ffprobe" -version > "$BUILD_DIR/evidence/ffprobe-version.txt" 2>&1
otool -L "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe" > "$BUILD_DIR/evidence/dynamic-libraries.txt"
shasum -a 256 "$PREFIX/bin/ffmpeg" "$PREFIX/bin/ffprobe" > "$BUILD_DIR/evidence/SHA256SUMS"
cp ffbuild/config.log "$BUILD_DIR/evidence/config.log"
cp config.h "$BUILD_DIR/evidence/config.h"
"$CC" --version > "$BUILD_DIR/evidence/compiler.txt"
sw_vers > "$BUILD_DIR/evidence/build-system.txt"
echo "Built: $PREFIX/bin/ffmpeg"
echo "Built: $PREFIX/bin/ffprobe"
