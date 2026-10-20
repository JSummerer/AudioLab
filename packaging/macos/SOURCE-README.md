# Exact sources for the macOS application dependencies

`sources.json` pins original HTTPS URLs, source archive names and SHA256 hashes.
The listed hashes were computed over the downloaded upstream artifacts; they
are integrity pins, not an independent upstream-signature attestation.

The `media` group builds FFmpeg 7.1 with LAME 3.100 and Opus 1.5.2.
`pkgconf` is an isolated build tool and is not shipped in the application.
The source archives include their original license and copyright texts.

To rebuild on an Apple Silicon Mac with Xcode Command Line Tools and Python 3.12:

```sh
AUDIOLAB_BUILD_PYTHON=/absolute/path/to/python3.12 bash packaging/macos/build_ffmpeg.sh
```

The default workspace is `.work/macos-package/media`; override with
`AUDIOLAB_MEDIA_BUILD_DIR`. It contains `downloads/`, extracted `source/`, an
isolated `install/` prefix and `evidence/`. `AUDIOLAB_BUILD_JOBS` permits 1–4 jobs.
The output is `install/bin/ffmpeg` and `install/bin/ffprobe`, targeting macOS 14
arm64. System SecureTransport and zlib remain system-library references.
The executable incorporates LAME/Opus statically. Rebuild all three from the
provided sources to replace either library; retain LGPL modification/relinking
rights. No proprietary object files are needed for these open-source programs.
When rebuilding from this source companion, copy its `sources/` files into the
chosen build directory's `downloads/` first; the script verifies those retained
archives and does not need to download them again.

The `soundfile` group is the exact source set named by SoundFile 0.14.0's pinned
libsndfile-binaries submodule `a3e6f9769d0c7e91d2d036cf0fdbe5b4bbf18b87`.
The installed libsndfile arm64 dylib matches that submodule byte for byte.
`libsndfile-mac_build.sh` and `libsndfile-darwin.cmake` preserve the original
upstream recipes (rename them to `mac_build.sh` and `darwin.cmake` in a working
directory when using them). They are provided as build provenance, not executed
by AudioLab's FFmpeg recipe. The original script has its own build prerequisites
(including CMake), download and unpack commands. Review platform/toolchain
compatibility before invoking it. Its pinned codecs differ from the new FFmpeg
codecs: do not substitute newer versions in the source companion for the
existing SoundFile binary. A modified libsndfile can replace the bundled dylib
through a documented rebuild and resign of the open-source AudioLab app.

The `certifi` group retains MPL-covered certifi 2026.10.19 sources.
`notice-overrides` holds exact source releases whose wheel omitted license files.
The `scipy-runtime` group retains GNU GCC 13.4.0 sources, the pinned Homebrew
Darwin patch and its build recipe. SciPy 1.18.1's three bundled runtime dylibs
match the compiler bottle's UUIDs and every file-backed Mach-O section hash;
their top-level file hashes differ because wheel repair changes load commands
and signing. Preserve GCC's runtime exception text with the original notices;
this does not assert that linking eligible compiler output requires making the
whole application GPL.
The `deno-reconstruction` group contains Deno 2.9.7, all source crates in its
release Cargo.lock, Rusty V8 150.4.0 and its pinned native sources, including
the LGPL glibc math component. This source superset supports reconstruction;
the application's notice index separately identifies the production Rust
dependency graph. [DENO-SOURCE-README.md](DENO-SOURCE-README.md) explains source
restoration, compilation and relinking, including modification of the math
component. The `deno-roots` group also identifies the original MPL-covered
Mozilla certificate sources. Deno's remaining covered Rust sources are retained
in the reconstruction set.
Original notices include Python 3.12.15/PBS 20261001's vendor license directory
and PYTHON.json, plus Deno's production Rust, V8/native, Rust runtime and
TypeScript notices. The standalone app's local ThirdParty index links these
component texts to the source companion accompanying its release.

Create a companion asset next to the app's release download:

```sh
python packaging/macos/third_party.py companion \
  --directory .work/macos-package/media/downloads \
  --output /path/to/NEW-third-party-sources.tar.gz
```

The command refuses to overwrite an existing source companion. `native-notices`
copies the compact media, SoundFile, certifi, GCC-runtime and root-certificate
notices; its default groups exclude the full Deno reconstruction sources.
The reviewed Deno notice asset is supplied separately. `notices` must run using the
clean bundle environment's Python and an explicit comma-separated list of
shipped distributions. Supply `--extra` once per native or override directory.
A missing wheel license is accepted only when an override directory named
exactly `<distribution metadata Name>-<version>` is explicitly provided.
Generate the user-facing notice/source index from the final package inventory;
retain this file and the complete sources alongside its release.
Raw build logs and evidence stay local; source companions include only pinned
source archives, recipes and original notices, with neutral archive ownership.
