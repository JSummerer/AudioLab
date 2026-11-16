# AGENTS.md

Instructions for AI coding agents working on this repository (Codex, Claude Code, Cursor, …).
People should start with [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
The rules here are shared; [CLAUDE.md](CLAUDE.md) adds only Claude-specific tooling notes. Keep both in sync
when a check, convention or safety rule changes.

## Repository
- This is the only repository, `JSummerer/AudioLab`. `main` is the latest release plus merged changes that are
  not released yet; those are listed under `## Unreleased` at the top of `CHANGELOG.md`.
- Work happens on short topic branches (`feat/…`, `fix/…`, `docs/…`). A branch reaches `main` only through a
  pull request merged with a merge commit (never squash or rebase); merged branches are deleted.
- Machine-specific notes live in `CLAUDE.local.md` (gitignored).

## What this is
AudioLab is a local, open-source audio app for novices up to experts: novices get a good result with no
knowledge, and expert controls (custom and section EQ, loudness, dynamics) sit under More options. It runs on
the user's own computer.
- **Save**: download a link's audio (yt-dlp) or convert files. **Enhance**: a content-aware mastering chain.
  **Split**: vocal/instrument stems (Demucs, optional AI pack). **Check**: analysis and quality report.
  **Library**: finished results with versions and a full-length original-vs-enhanced compare.
- One Python package `audiolab/`: a FastAPI server, a plain-JS web UI (`audiolab/web/`), a CLI. On macOS it opens in
  its own window (pywebview); elsewhere in the browser. `docker/` runs it as a server.
- Status: public beta. macOS first; keep Windows/Linux working (all OS code lives in one module). The version
  is plain MAJOR.MINOR.PATCH in `audiolab/__init__.py`; "beta" is said in the release title, not the number.
  Release notes are in `CHANGELOG.md`, later ideas in `docs/ROADMAP.md`.

## Read before larger changes
- `docs/ARCHITECTURE.md`: module map, life of a job, how to add a recipe/format/task/view/OS, data locations.
- `docs/API.md`: the HTTP contract between the UI and the server. The UI is built only against it.
- `CONTRIBUTING.md`: setup, checks, principles.

## Setup and run
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Python 3.10–3.13 (add ,ai for Demucs/torch)
.venv/bin/audiolab              # the app (macOS: native window; elsewhere: browser)
.venv/bin/audiolab serve --port 8790                            # server only
```
`./start.sh` is the end-user launcher: it builds `.venv` with uv and installs `~/Applications/AudioLab.app` on macOS.
The web UI has no build step, so reload the page after editing.

## Checks: run all of them before you call a change done
```bash
uvx ruff check . && uvx ruff format --check .        # or .venv/bin/ruff from the dev extra; `ruff format .` fixes
.venv/bin/python -m pytest                           # local HTTP fixtures, no downloads or AI models required
find audiolab/web -name '*.js' -exec node --check {} \;   # JS syntax (Node 22+)
bash -n start.sh                                     # when you touched the launcher
```
- Tests isolate data and output folders through `tests/conftest.py`. Add tests for new server behaviour next to
  the existing ones (`tests/test_library.py` shows the fake-runner and TestClient patterns).
- Pytest also runs `tests/js/*.test.mjs` with Node 22+; without it the browser regression check skips.
  These use small DOM/media fakes, not a personal browser. Keep Node installed when running all checks.
- `tests/test_docs.py` keeps the docs honest: relative links, API routes, settings, split formats and the version
  example are checked against the code. Update the docs when it fails, not the test.
- The suite has about 675 tests (November 2026): more than a production release needs, but fine for
  development. Trim it before a production release (see `docs/ROADMAP.md`).
- CI (`.github/workflows/ci.yml`) runs Python 3.12 checks on macOS, the supported platform, for every pull
  request. Started by hand, it also checks Windows and Linux; do that before a release when they need checking.
  It never runs on pushes.
- The published AI add-on is pinned to the exact bytes of its inputs (`packaging/ai/published-helper.json`:
  `system.py`, `errors.py`, `context.py`, `runtime.py`, `audiolab/ai/`, …). Editing one makes the Mac build refuse
  to reuse it, which would force a new AI version and a 570 MB re-download for users. Put new code elsewhere
  unless a new AI version is intended.
- Managed-AI regressions use tiny generated archives and localhost HTTP fixtures. Actual frozen helper/app
  checks belong in `packaging/ai/` and `packaging/macos/`; publish only the archive pinned by the app catalog.

## Conventions
- Python: ruff config in `pyproject.toml` (single quotes, 120 columns), Python 3.10–3.13, type hints and comment
  density like the surrounding code.
- **OS-specific code lives only in `audiolab/system.py`** (and `audiolab/trash.py` for the Trash, kept apart because
  the published AI add-on is pinned to `system.py`'s exact bytes). Other modules call their functions instead of
  checking `sys.platform`.
- User-facing failures raise `AudioLabError(message, hints, fix)` in plain language, with `hints` a novice can
  act on.
- Long work stays cancellable: report through `Progress`/`check_cancel`; run subprocesses via `media.proc.run`.
- Never overwrite files (`util.unique_path`). Audio stays float32 and streamed so 2-hour sets fit in memory.
- Formats: `FormatSpec` in `media/export.py` is the one source of truth, including the facts the UI shows via
  `/api/health`. Don't hard-code format text in the UI.
- Brand and name: `APP_NAME`/`APP_SLUG` in `audiolab/__init__.py`. See "Renaming" in ARCHITECTURE.md.
- Web UI: vanilla ES modules, no framework, no build, no npm dependencies.
  - Views export `{ el, heading, mount, show, hide }` and stay mounted; shared controls are in `js/ui.js`.
  - Every `localStorage` access goes in try/catch.
  - Copy is short and plain; controls are keyboard and screen-reader friendly.
- **Design ("Sleeve", 2.4):** a white sheet on a grey desk (the sidebar), like a record sleeve's liner notes.
  - Values come from `css/tokens.css` only: colours (light and dark), the type sizes, the spacing scale `--s-1`…
    `--s-7` (4–48 px) and the radii. No new hex values, font sizes or shadows in view CSS.
  - Serif (`--font-serif`) only for page titles, section heads and record titles; everything else in the system
    font. The serif is New York where `ui-serif` exists (Safari, the Mac window), else the bundled Source Serif 4
    (`web/fonts/`, one weight): change fonts only in `tokens.css` + `web/fonts/`, never per view. No monospace except
    raw tool output (logs). Accent for actions and the selected state, teal (`--ok`) for done, orange (`--live`)
    only for something running or playing.
  - Simple choices are compact: one row of pills (`segmented()`) with one note line for the picked option;
    several choices are a checklist (`chips()`); on/off is a switch. No big tiles, no lone item on a row.
  - Folded parts use one disclosure style (`details.disclosure` or `button.more-toggle` with `aria-expanded`);
    show/hide is a disclosure, not a switch.
  - Lists are rows with hairlines, not cards; only real objects (cover, dialog, menu, info card) get a shadow.
    One primary button per screen; secondary actions are `.btn-key`, minor ones `.btn-ghost`.
  - Avoid: boxes inside boxes, uppercase or monospace labels, a new look for "selected", raw paths (use
    `shortFolder()`), and anything below WCAG AA contrast.
- Keep it simple. It's "a GitHub repo people download and run": no new dependency without a strong reason, and
  no speculative abstractions.

## Keep in sync when you change something
- Endpoint, field or setting: update `docs/API.md` (and tests) in the same change. Settings need the default in
  `settings.py` and a row in the Settings view.
- New module or a module's role: update `docs/ARCHITECTURE.md`.
- User-visible behaviour: add it under `## Unreleased` at the top of `CHANGELOG.md` (add the heading if it is
  missing); a release turns that section into its entry. Bump `__version__` only for a new release.

## Leave the checkout clean
- Temporary files (scripts, test renders, reports, screenshots) go in a temp folder outside the repository.
  Delete anything you created inside the repository (test output, `.playwright-mcp/`, stray files) before you finish,
  and check `git status --short --ignored` against how the folder looked when you started.
- No plans, handoffs, audit notes or listening kits in the repository. Plans belong in the conversation,
  results in commits, and lasting decisions in the docs or `CHANGELOG.md`.
- `.work/` is only the packaging scripts' build workspace (`packaging/ai/`, `packaging/macos/`), recreated
  by them as needed.

## Safety
- **Agents never delete or overwrite the user's output files.** The app itself follows the user's choice:
  - Defaults keep everything; "Remove from Library" alone only forgets the entry.
  - Deleting is an explicit opt-in by the user (e.g. "Also move its files to the Trash", "Keep only this
    version"), always to the system Trash, never a permanent delete. These are user choices, not fixed rules
    to design around.
  - The one exception: a job that fails or is cancelled while it is still working removes the files and the
    folder that same attempt created (`outputs.py`), checked by file identity. A finished result is never
    removed this way.
  - Kept originals live only in `<data>/library/originals/` and are cleared only by explicit user actions.
- `archive/`, `deliverables/`, `runs/`, `output/` and `data/` are gitignored local folders that may hold the
  user's real audio: read-only, never move or delete them.
- Smoke tests only, no load or stress tests:
  - Start a throwaway server with `AUDIOLAB_DATA_DIR` and `AUDIOLAB_OUTPUT_DIR` pointing to a temp folder.
  - Use a free port, not 8765: that's the app's default and may be the user's running app.
  - Feed it short generated test tones, no large downloads, and stop only the process you started.
- Browser checks:
  - Use a separate headless test browser, such as Playwright's own; never the user's real browser, profiles or
    cookies.
  - YouTube sign-in cookies are only read through the app's opt-in setting.
- On the user's machine, don't trigger OS permission prompts (screen recording, accessibility or UI scripting)
  and don't steal focus.
- Git: don't commit or push unless asked.
  - Changes reach `main` only as pull requests: `git push -u origin <branch>`, `gh pr create --base main` with a
    body saying what changed and which checks passed, wait for CI, then `gh pr merge --merge --delete-branch`.
    Never squash or rebase, and never push directly to `main`.
  - A release is a version bump in its pull request, a tag `vX.Y.Z` on the merge commit, and a normal GitHub
    release (titled "… (beta)" during the beta) with the Mac build started by hand.
  - The `v2.4.0b2` release hosts AI pack 1.0.0, which every current app downloads: never delete or edit that
    release or its assets. The old `v2.4.0b*` tags stay.
  - Test any build with `AUDIOLAB_DATA_DIR` and `AUDIOLAB_OUTPUT_DIR` set to temporary folders, so it never
    touches an installed app's data.
  - Audio, app data, environments and generated builds stay local and gitignored.
