# Contributing

Thanks for helping! A few ground rules keep AudioLab friendly for people who are not audio engineers.

For everyday use rather than development, follow the [installation guide](docs/INSTALLATION.md).
Mac testers can use the [DMG](docs/MACOS.md); development still runs from source.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Python 3.10–3.13; add ,ai for the AI pack
.venv/bin/audiolab                 # run the app (native window on macOS, browser elsewhere)
.venv/bin/audiolab serve           # server only, open http://127.0.0.1:8765 yourself
```
On Windows use `.venv\Scripts\` instead of `.venv/bin/`. The UI is plain HTML/CSS/JS in `audiolab/web/` —
no build step; reload the page after editing.

## Checks

CI runs on public pushes to `main`, pull requests and manual starts, using Python 3.12 on macOS,
Windows and Linux. These are generated-signal and unit/integration tests; they do not establish that
the native UI, fresh-machine setup or optional AI work on every computer.
CI also repeats comparison-playback regressions with the bundled FFmpeg, so an installed system
version cannot hide differences in the fallback used by a fresh installation.

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint + formatting (config in pyproject.toml)
.venv/bin/python -m pytest                               # local HTTP fixtures, no downloads or AI models
find audiolab/web -name '*.js' -exec node --check {} \;      # JS syntax (Node 22+)
bash -n start.sh && bash -n AudioLab.command                 # after touching the launchers
```
`ruff format .` fixes the formatting. AI/model tests are opt-in (`AUDIOLAB_RUN_AI_TESTS=1`); normal tests
generate their inputs and do not need private recordings or network downloads.
With Node 22+ installed, pytest also runs the dependency-free JavaScript regressions in `tests/js/`
(otherwise that check is reported as skipped). They exercise state races and playback failures without
a browser, profile or network. Browser smoke tests still need a separate headless browser.

## Reporting and changing things

Open an [issue](https://github.com/JSummerer/AudioLab/issues) with the expected behavior, what happened,
steps to reproduce, OS and AudioLab version. For a Git checkout, include `git rev-parse --short HEAD`.
For a downloaded app, include the release tag, exact asset name, Mac chip and macOS version. The
Mac beta remains a prerelease; build checks and publication status belong in its release
notes. Use generated examples or recordings you may share, and remove personal
paths, links and credentials from logs. Report security issues [privately](SECURITY.md).

Keep pull requests focused, explain the user-visible change and how you checked it, and update the
relevant documentation and changelog. A brief issue first is useful for a large change.
Contributions are distributed under the project's [MIT license](LICENSE).

## Where things go
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the module map, the life of a job, and how to add a
recipe, format, task, view or support for another OS. The HTTP contract between UI and server is
[docs/API.md](docs/API.md); keep it in sync when you change an endpoint.

## macOS binary delivery

The binary targets macOS 14+ arm64. Its main download includes Save/Convert, Check and
Light/Standard Enhance. Settings manages a separate immutable AI helper archive for Split and
Best Enhance, with both offline models. The app never runs pip or changes shared environments.
Source installation and the Windows/Linux browser paths remain separate.
See [packaging/macos/README.md](packaging/macos/README.md) for the pinned build environment, selected
FFmpeg sources/licenses, app entry, smoke checks and release artifacts.

Run the normal checks before artifact checks. Validate the exact frozen executable with generated
short audio and isolated data/output directories; testing source Python alone cannot establish that
the bundle includes every module, tool, certificate or resource. Native WebKit checks belong on a
disposable runner/test user, with no personal browser profiles or recordings. State OS, architecture,
artifact identity and residual limits. Intel output from an experimental workflow is not qualified
support until it passes its own native checks.

Distribute the DMG, exact AI archive/catalog, checksums and both matching third-party source
companions through a GitHub prerelease. The app embeds the AI archive hash and versioned URL;
upload these assets together and check the public installation path after publication.
Do not include `.work/`, local audit evidence, private recordings, build environments or old private
Git history. Keep the bundle identifier `io.github.jsummerer.audiolab` stable. This beta uses ad-hoc
signing without Apple Developer ID/notarization; document [Apple's graphical opening
flow](https://support.apple.com/en-us/102445), and do not claim ordinary CI proves fresh-download
Gatekeeper acceptance. Downloaded apps update by replacement, not in-place package installation.

## Principles
- **Plain language first.** Every error is an `AudioLabError` with a short message and `hints` a
  novice can act on. Numbers and jargon belong under **More options** or **Show details**.
- **Explain automatic decisions.** Anything chosen automatically gets a one-line `why`.
- **Preserve by default.** New processing must not make a good recording worse; bold moves are opt-in
  or limited to the recipe that needs them.
- **Never lose user files.** No overwriting (use `util.unique_path`), no deleting outside job folders.
- **Stay cancellable.** Long loops report through `Progress` (which raises `Cancelled`); subprocesses go
  through `media.proc.run`.
- **Bounded memory.** Keep audio float32 and stream long signals (see `dsp/filters.filtfilt_stream`).
- **OS differences live in `system.py`.** Other modules call its functions instead of checking the platform.
- **Small and dependency-light.** No UI framework or build step; a new dependency needs a good reason.

## Credits

AudioLab's code is available under the [MIT license](LICENSE). Dependencies retain their own licenses.
Built with [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org),
[Demucs](https://github.com/adefossez/demucs), [Deno](https://deno.com), NumPy/SciPy, soundfile,
FastAPI and pywebview. The bundled Source Serif font includes its [license](audiolab/web/fonts/OFL.txt).
The Mac app includes dependency notices and a matching source companion in its release.
Source installations use [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) as a fallback audio engine.
