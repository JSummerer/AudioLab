# Building the macOS tester app

This folder builds the **core** application: Save/Convert, Check and Light/Standard Enhance.
It bundles Python and native tools. Testers install neither Python nor uv. The optional AI runtime
is excluded, and the app explains that Split and Best enhancement are unavailable.

The **Build macOS tester app** workflow in GitHub Actions is the maintained release path. Start it
manually from the Actions tab; it creates build artifacts without publishing a release.
Temporary app/source artifacts expire after one day; validation logs expire after seven days.
Download and verify them before publishing the beta's durable assets in GitHub Releases.

For a local build, use an Apple silicon Mac with Xcode Command Line Tools,
[uv](https://docs.astral.sh/uv/getting-started/installation/) and the exact Python Build Standalone
runtime selected in that workflow: CPython **3.12.15, build 20261001**. The build checks this identity
because the retained runtime notices must match it. Set `AUDIOLAB_BUILD_PYTHON` to that interpreter:

```sh
bash packaging/macos/build.sh
```

The build uses `.work/macos-package/` for its environment, downloads, media compilation, freezer
cache and release products. Override `AUDIOLAB_PACKAGE_WORK_DIR` for a separate build. Set
`AUDIOLAB_BUILD_PYTHON` to select the pinned builder interpreter. Nothing is installed globally.
Use a fresh work folder when rebuilding a prepared release. Requirements are pinned in
`requirements.lock`; the application source comes from this checkout.

The result is a directory-style `AudioLab.app`, a compressed DMG with an Applications shortcut,
checksums and a companion archive of corresponding third-party sources and build recipes.
The app is ad-hoc signed, **not Developer ID signed or Apple-notarized**. This is deliberate for
the small tester beta; see [Mac installation and uninstall](../../docs/MACOS.md).

`build_ffmpeg.sh` builds the selected LGPL audio engine from checked source archives rather than
redistributing the unrelated FFmpeg executable in the imageio wheel. The freezer excludes that
wheel's runtime and Mutagen; AudioLab uses its own FFmpeg path for media tags and covers. Original
dependency notices are embedded in the app, with source provenance in the release companion.
The source Python app retains its existing dependencies and optional AI install workflow.

The smoke check exercises the **frozen executable**, generated three-second audio, each core
conversion format, Standard Enhance, Check, a localhost link, and HTTP/UI assets. It removes
external Python from PATH and uses isolated data/output folders. It does not test real websites.
The optional native WebKit smoke command is restricted to disposable GitHub Actions runners so
local verification does not steal focus or write to a user's WebKit profile.

Before uploading a binary, require the ordinary repository checks, build checks and actual artifact
smoke results. Upload the matching source companion and checksums alongside the DMG in a GitHub
prerelease. Do not attach build folders, logs containing personal paths, local audit reports or any
real recordings. Native CI is regression evidence; it does not qualify every macOS version or
the manual Gatekeeper approval interaction.
