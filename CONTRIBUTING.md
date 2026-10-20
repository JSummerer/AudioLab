# Contributing

Thanks for helping! A few ground rules keep AudioLab friendly for people who are not audio engineers.

For everyday use rather than development, follow the [installation guide](docs/INSTALLATION.md).

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

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .   # lint + formatting (config in pyproject.toml)
.venv/bin/python -m pytest                                    # no network, no AI models needed
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
The current development source still reports package version 2.3.0; unreleased changes are collected
under 2.4.0 in the changelog. Use generated examples or recordings you may share, and remove personal
paths, links and credentials from logs. Report security issues [privately](SECURITY.md).

Keep pull requests focused, explain the user-visible change and how you checked it, and update the
relevant documentation and changelog. A brief issue first is useful for a large change.
Contributions are distributed under the project's [MIT license](LICENSE).

## Where things go
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the module map, the life of a job, and how to add a
recipe, format, task, view or support for another OS. The HTTP contract between UI and server is
[docs/API.md](docs/API.md); keep it in sync when you change an endpoint.

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
