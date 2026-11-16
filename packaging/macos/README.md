# Building the macOS tester app

This folder builds the application and its separate optional AI add-on. The main app bundles Python
and native tools for Save/Convert, Check and Enhance with "Volume only" or "Tone and volume". Settings
can download the compatible add-on for Split and AI stems Enhance; testers install neither Python nor uv.

The **Build macOS tester app** workflow in GitHub Actions is the maintained release path. Start it
manually from the Actions tab; it creates build artifacts without publishing a release.
Temporary app/source artifacts expire after one day; validation logs expire after seven days.
Download and verify them before publishing the release's durable assets in GitHub Releases.

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
`requirements.lock`; the application source comes from this checkout. Refresh `yt-dlp` there for every
release: the built-in downloader is what every user starts with and falls back to, and sites change often. It has no
`sources.json` row, so this does not touch the AI helper's pin. The spec also freezes standard-library
modules nothing in the app imports yet (`DOWNLOADER_STDLIB`), so a downloaded downloader update that
starts using one still runs.

CI seeds `media/downloads/` from the checksum-pinned `v2.4.0b2` core source companion to avoid
downloading unchanged sources from every upstream host again. `seed_sources.py` copies only regular
`sources/<filename>` members whose bytes match the **current** `sources.json`; archived build helpers
are ignored. Missing or changed sources use normal acquisition, which still verifies every input.
The seed download and outer checksum must succeed; an unexpected existing cache file is never overwritten.

The result is a directory-style `AudioLab.app`, a compressed DMG with an Applications shortcut,
the core source companion/build recipes, the AI catalog and checksums. An explicit helper rebuild
also produces its new AI runtime archive and matching source companion.
The [AI packaging step](../ai/README.md) runs first. By default it reuses the pinned **AI 1.0.0**
catalog, runtime archive and source companion from `v2.4.0b2`, retaining their exact original
bytes and release URL. It verifies their hashes/sizes and the helper execution/build input
contract against that source companion. Thus core-only releases do not make users reinstall
an unchanged helper. CI fetches the original assets with bounded retries; locally, set
`AUDIOLAB_AI_REUSE_DIR=/path/to/retained/release` to use existing checked assets. The directory
must contain `ai-catalog.json` and both original AI archives. The build work folder retains them
for smoke checks. A reuse release contains the new core DMG/source companion, the unchanged AI
catalog and checksums; it does not duplicate the old AI archives in CI artifacts or releases.
Keep the original public helper release and its matching source companion available.

If the helper contract changes, reuse stops before downloading assets. Intentionally rebuild
with `AUDIOLAB_AI_MODE=rebuild AUDIOLAB_AI_VERSION=<new-version>`; the already published helper
version is refused. Validate and publish the new helper and its matching source companion,
then review/update the pin as described in the AI guide. Do not retag the old catalog or replace
published AI bytes. Testers download only the DMG; Settings handles the pinned AI asset.
The app is ad-hoc signed, **not Developer ID signed or Apple-notarized**. This is deliberate for
the small tester beta; see [Mac installation and uninstall](../../docs/MACOS.md).

`build_ffmpeg.sh` builds the selected LGPL audio engine from checked source archives rather than
redistributing the unrelated FFmpeg executable in the imageio wheel. The freezer excludes that
wheel's runtime and Mutagen; AudioLab uses its own FFmpeg path for media tags and covers. Original
dependency notices are embedded in the app, with source provenance in the release companion.
The source Python app retains its existing dependencies and optional AI install workflow.

The core smoke check exercises the **frozen executable** with short generated fixtures. It checks
each core conversion format, Enhance with "Tone and volume", Check, a localhost link, and HTTP/UI assets.
The HTTP check saves a result made with "Tone and volume" and checks that the Library retains its
reduction timeline after job removal. It auditions a five-second excerpt with changed settings and applies those settings to
the full recording. It checks the two bounded PCM streams, new exports in the same Library group,
and the kept original's hash. The check removes external Python from PATH and uses isolated
data/output folders and localhost requests. It also runs the frozen downloader mode with offline
stand-in updates in the data folder: a newer one runs, a broken or an older one falls back to the
built-in copy.
The AI check installs the real archive through the production manager with a localhost transport
fixture, then exercises frozen Split, AI stems Enhance, removal and reinstallation. The independent
helper check runs for both reused and new archives, exercises both offline models using the
helper's selected device (MPS where available), and verifies that a worker keeps the installation locked if
its parent dies. Check installation from the actual public HTTPS release URL after publication.
The optional native WebKit smoke command is restricted to disposable GitHub Actions runners so
local verification does not steal focus or write to a user's WebKit profile.

Before uploading a binary, require the ordinary repository checks, build checks and actual artifact
smoke results. Upload the DMG, new core source companion, AI catalog and checksums to the GitHub
release (a normal release for X.Y.Z; the in-app check reads the version, not GitHub's prerelease flag:
a final build is offered only final versions, a beta-numbered build also betas). A helper rebuild additionally includes its new runtime and matching source companion;
a reuse release keeps linking to the original published helper assets. Do not attach build folders, logs containing personal paths, local audit reports or any
real recordings. Native CI is regression evidence; it does not qualify every macOS version or
the manual Gatekeeper approval interaction.
