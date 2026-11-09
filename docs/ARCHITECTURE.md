# Architecture

AudioLab is one Python package (`audiolab/`) that runs a local web server and a
plain HTML/JS front end. The same task code powers the app window, the command
line and the Docker image.

```
     app window (macOS: native) / browser                  terminal
                     │  HTTP + SSE (docs/API.md)              │
              ┌──────▼───────┐   desktop.py starts it   ┌─────▼─────┐
              │    api/      │◄──────────────────────── │  cli.py   │
              └──────┬───────┘                          └─────┬─────┘
                     │ submit / cancel / retry                │ runs a task inline
              ┌──────▼───────┐                                │
              │   jobs.py    │  queue · worker · job.json · journal.jsonl
              └──────┬───────┘                                │
                     └──────────────┬─────────────────────────┘
                             ┌──────▼──────┐
                             │   tasks/    │ download · enhance · separate · analyze · import
                             └──────┬──────┘
     ┌────────────┬─────────────────┼──────────────┬──────────────┐
┌────▼────┐ ┌─────▼─────┐   ┌───────▼──────┐ ┌─────▼─────┐ ┌──────▼─────┐
│ media/  │ │ analysis/ │   │  recipes/    │ │   ai/     │ │ previews.py│
│ yt-dlp  │ │ profile   │   │  params +    │ │ Demucs    │ │ excerpt for│
│ ffmpeg  │ │ classify  │   │  one chain   │ │ (optional)│ │ comparing  │
│ export  │ └─────┬─────┘   └───────┬──────┘ └───────────┘ └────────────┘
└─────────┘       └──────── dsp/ ───┘   filters · bus · loudness · limiter · dither · denoise · exciter · deess

   system.py — the only module that knows the operating system (used by all of the above)
```

## Modules

| Module | Responsibility |
|---|---|
| `__init__.py` | Brand (`APP_NAME`, `APP_SLUG`) and version — see "Renaming" below |
| `desktop.py` | Starts the app: picks a free port, runs the server, opens the native window (pywebview; default on macOS) or the browser, one instance per data folder, clean quit |
| `runtime.py` | Source/frozen-runtime boundary: packaged downloader command, private bundled-tool lookup, certificate trust and app-owned cache/temp setup before helpers start; source mode retains its existing environment |
| `cli.py` | `audiolab [app\|serve\|download\|enhance\|separate\|analyze\|doctor\|update\|install-ai\|shortcut]` |
| `api/` | FastAPI app (`app.py`: `create_app`, lifespan, error format, static UI), `security.py` (local-mode Host/Origin checks, token, upload cap), `state.py` (per-app runtime state: manager, live-event hub, caches, health), `jobs.py` (probe + job routes, files, ZIP, excerpt PCM), `status.py` (health, settings, storage, diagnostics, maintenance), `events.py` (SSE), `library.py` (Library list and entries, files, PCM chunks for comparing, removal) |
| `jobs.py` | Job records, FIFO queue, worker threads, cancel/retry, restart recovery, event fan-out, the one bounded `shutdown()` |
| `library.py` | The Library: one JSON record per finished result (Save, Enhance, Split) in `<data>/library/`, independent of job retention; keeps the acquired original of Enhance jobs for comparing (hard link, or a copy where links are impossible, named by its content's SHA-1; setting `keep_originals`); groups versions of one recording (video id, link, or an upload's content); picks the result an entry is compared with (`compare_file_id`, lossless first); removing an entry keeps output files unless the user opts in to the Trash |
| `context.py` | `Progress` (scoped progress, journal notes, cancellation, child-process registry) and `TaskContext` (stages of one job) |
| `tasks/` | The four user-facing tasks (`validate`, `stages`, `run`) and `importing.py`, the `import` task behind Library "Add a file" (`import` is a Python keyword, so `tasks/__init__.py` maps task names to modules); `common.py` acquires sources (reusing a kept original for a link when allowed) and publishes outputs; `preview.py` runs bounded Library section previews through the existing Enhance queue |
| `media/` | `proc.py` (cancellable subprocesses), `ffmpeg.py` (system or bundled ffmpeg, probing incl. embedded tags, `decode_pcm`: 48 kHz chunks on a 1/25 s grid that join like one decode; WebM/Matroska's millisecond timestamps get a seek offset measured once per file), `youtube.py` (yt-dlp + deno, the artist/title rule for links, tracks from chapters/tracklists, self-repair update, browser sign-in), `export.py` (format matrix, the file types that can be added, tags, cover art, sample-accurate track cutting) |
| `analysis/` | `profile.py` (loudness, true peak, LRA, bandwidth, bands, alerts), `levels.py` (bounded whole-reference level scan), `tone.py` (how the tonal balance moves, chart data from the same scan), `classify.py` (content type + reasons), `key.py` (musical key estimate) |
| `recipes/` | `Recipe` + `ChainParams`, `resolve()` turns recipe + measurements + user overrides into parameters *with reasons*; `chain.render()` is the single mastering chain |
| `ai/` | Lazy source torch/demucs detection and windowed separation; `install.py` installs source packages, `managed.py` owns the catalog-pinned packaged runtime lifecycle selected by `api/state.py`, `client.py` invokes the isolated helper, `worker.py` runs/probes it and loads local offline weights |
| `dsp/` | Float32 signal-processing blocks; `scratch.py` reuses disk-backed full-rate buffers for Enhance beyond 10 minutes; `timeline.py` collects bounded reduction highlights without retaining full-rate controls |
| `previews.py` | The Enhance before/after excerpt (a loud 45 s passage, or the whole recording when shorter) at the true relative level, one shared headroom gain; compared when the original isn't kept |
| `processing.py` | Bounded, path-free projection of resolved and applied Enhance processing for results and durable Library history; legacy entries remain readable |
| `settings.py` | Data/output folders (the output folder is only resolved here, never created: whatever writes creates it), env-var limits (`AUDIOLAB_*`), persisted user preferences (checked on load/update; a new snapshot becomes active only after saving succeeds) |
| `resources.py` | Memory/disk/duration preflight, run before any big download; also refuses an output folder that can't be written to |
| `diagnostics.py` | Log file setup and the plain-text system/job report for bug reports (`/api/diagnostics`, `audiolab doctor --report`) |
| `errors.py` | `AudioLabError(message, hints, fix, detail)` — every user-visible failure carries plain-language hints, an optional one-click fix and the raw text behind it (`detail`, for bug reports only); `storage_error()` words a full disk or a folder AudioLab may not write to. Anything else unexpected becomes a JSON 500 with hints (`api/app.py`) or "Something went wrong while …" on the job (`jobs.py`) |
| `util.py` | File-name safety, unique paths, hard links, media types, and human-readable sizes and durations |
| `system.py` | **The OS layer** — data/log folders, whether a folder is writable, reveal in Finder/Explorer, move to the Trash (only on the user's opt-in), notifications, keep-awake, single-instance lock, process groups, memory size, file-name limits, app identity and shortcut. A leaf module with no third-party dependencies |
| `assets/` | App icon |
| `web/` | The UI (see below) |

Resource checks are estimates, not memory reservations. Native-host RAM uses 85% of installed memory,
without subtracting other apps or explicitly budgeting model-specific GPU allocations. Container checks
use the cgroup limit minus its reported usage before applying that fraction. See
[hardware guidance and limits](HARDWARE.md#what-the-app-checks); AI's windowed separation does not make
the complete Enhance chain constant-memory. Disk estimates depend on task/duration/quality, not every
source rate, export format or output count; the added free-space allowance is not reserved capacity.
Long Enhance adds up to 36 bytes per 48 kHz frame (about 5.8 GiB/hour) for disk-backed working buffers.
The earlier conservative RAM estimate remains in force pending representative long-run measurements.

### Platform code
Everything that differs between macOS, Windows and Linux lives in `system.py`; other modules call its
functions and never branch on the platform themselves. The only exceptions are the Windows ffmpeg shim
in `media/ffmpeg.py` (the `.exe` suffix and copying instead of symlinking), and `cli.py`, which prints
the platform in `doctor`. Porting a feature to another OS means filling in its branch in `system.py`.

### Self-contained macOS Core build

The source launchers remain for development and the source-mode AI installer. The downloadable Core app has a separate
entry, `packaging/macos/entry.py`, frozen with the spec and pinned build inputs in `packaging/macos/`.
It starts the same native window/server/task pipeline without `start.sh`, uv, pip or a user's Python.
Its private downloader mode dispatches yt-dlp inside the frozen executable; that executable is not a
general-purpose Python interpreter. `runtime.py` chooses this mode and the bundled FFmpeg/Deno.

The main app exposes Save/Convert, Check and Volume only/Adaptive Enhance without AI. Since version 2.4.0b2 it
offers a managed optional add-on containing a frozen AI helper and both
supported model choices; the main app's bundled Python packages remain immutable. `ai.install_supported`
is driven by a build-generated, bundled catalog, not simply `packaged`. Split/AI stems use the healthy
managed helper when present. Source/CLI/Docker keep their existing optional-AI and update paths.
Downloader self-updates remain disabled in packaged mode; replace the app to update the main delivery.

Core builds normally reuse the immutable helper selected by `packaging/ai/published-helper.json`.
`packaging/ai/reuse.py` checks the exact catalog/archive/source companion and the reviewed helper
execution, protocol, model and build inputs before running its smoke checks. App version changes alone
do not change the helper catalog, so an installed compatible add-on remains usable after an app update.
A changed helper input requires an intentional new helper version and reviewed pin; see the
[AI packaging guide](../packaging/ai/README.md). The original published assets must remain available.

`ai/catalog.json` pins one compatible HTTPS release asset, archive checksum, exact byte counts,
protocol, executable and model choices. No mutable latest-version lookup or user-supplied download URL
is used. `ai/managed.py` downloads resumably into `<data>/ai/download.part`, verifies size/hash, extracts
into owned staging, rejects unsafe archive entries, probes the helper and atomically publishes an
active receipt. It preserves the prior installation until the replacement passes. Repair rebuilds
the selected version; cancel leaves a resumable partial; remove clears owned AI files only. Runtime,
weights, cache and temporary files stay under `<data>/ai/`; shared/source caches are neither adopted
nor deleted. Source `install.py` keeps the existing pip/uv route.

An ownership marker distinguishes a managed root from unrelated existing files. Output folders must
not overlap AI storage; settings and task output creation both enforce this. Archive extraction accepts
regular files, directories and deferred hardlinks to direct regular archive members, never symlinks or
link chains. This stores shared native libraries once; storage reporting also counts their inodes once.

AI jobs and lifecycle operations share `<data>/.ai-operation.lock`, outside the removable root. A job
lease is inherited by the helper process so a parent crash cannot allow removal while it still uses
the runtime. `ai/client.py` runs the frozen `AudioLabAI` helper through `media.proc.run` with progress,
cancellation and output-path checks. `ai/worker.py` speaks a versioned JSON-lines protocol and loads
pinned local safetensors models with no remote fallback. Health and storage report the owned files
separately from the catalog's unpacked target size. See the [HTTP contract](API.md#health).

The initial release target is macOS 14+ arm64 with identifier `io.github.jsummerer.audiolab`. The build
produces an ad-hoc-signed `.app`/DMG, checksums, the AI catalog and a matching core source companion.
An intentional helper rebuild also produces new AI runtime and source archives; reuse keeps their original published URLs.
It has no Developer ID or Apple notarization. The build's selected FFmpeg is compiled from its
recorded LGPL sources rather than using the source setup's imageio fallback. CI seeds its source cache
from a checksum-pinned published companion using `packaging/macos/seed_sources.py`; only source archives
matching the current manifest are reused. Missing or changed inputs still go through the normal fetch checks. See the
[packaging guide](../packaging/macos/README.md) for build inputs, licensing and artifact checks, and
the [Mac guide](MACOS.md) for the tester's installation/update/uninstall path. Native artifact checks
and fresh-download Gatekeeper checks are separate from ordinary source regressions.

## The web UI (`audiolab/web/`)

No framework, no build step: ES modules loaded straight by the browser (reload to see changes).

```
index.html            the shell: sidebar (collapses to an icon rail, ⌘\ / Ctrl+\; bottom tab bar on narrow screens) + one <section> per view
css/                  tokens.css (colours, type, spacing; light/dark) · base.css (reset, shell) · components.css (shared controls)
                      · one file per view: new.css · activity.css · job.css · library.css (+ the compare player) · settings.css
fonts/                the bundled title serif (Source Serif 4, OFL) for browsers and systems without ui-serif; README says how to swap it
js/main.js            boot, global wiring (paste or drop a link anywhere, drop files, deep links ?url=&task=, notice banner)
js/router.js          hash routes #/new · #/activity · #/job/<id> · #/library[/<entry id>] · #/settings[/<section>]
js/api.js store.js    HTTP client + token · app state, event bus, live updates (SSE, polling fallback)
js/dom.js ui.js       tiny DOM helpers and icons · shared controls (toasts, dialogs, menus, meters, segmented…)
js/player.js          job page players: stems, tracks, one file (says when this browser can't play its format)
js/compare.js         the compare player (job page and Library): original | result sample-locked on one Web Audio clock from
                      server-decoded PCM chunks, crossfaded switch, whole-song volume match · plain Library file player
js/actions.js         job actions shared by views (cancel, retry, remove, clear finished…)
js/quick-preview.js   Library "Preview changes": a 5–30 s passage of the kept original with changed settings, then Apply
js/eq.js              custom EQ as text (parse/format bands, the two presets, the shared editor in Fine-tuning and previews)
js/section-eq.js      section EQ: the same checks as the server, the section editor (Fine-tuning, previews)
js/tone-chart.js      the tone timeline chart (Check, Enhance, Library previews; a click picks a preview start)
js/views/new/         the "New" view: source (link/files/playlist), names, cover, task + options, tracks, start, and the
                      sleeve beside them (what is being made + the start row; a bottom bar when narrow)
js/views/activity.js  the job list (in progress, then finished by day; filters)
js/views/job/         one job: live progress, failures with fixes, results per task
js/views/library.js   finished results by recording: versions, files, play/compare, kept originals, remove
js/views/settings.js  preferences and system status
```

Views stay mounted; the router shows one at a time and calls its `show`/`hide` hooks (a hidden job
page stops its players and polling). There is no global Simple/Advanced mode: each task in New folds its
rarely used choices and fine-tuning under **More options** (folded, it says how many differ from their
defaults, with Reset), and whatever is set there is sent with the job. Job pages have a **Show details**
switch (numbers, decisions, log details). Both remember open/closed in the browser (`localStorage`; the macOS
window keeps it too: `desktop.py` starts pywebview with `private_mode=False`); the one
Settings row "Show all options by default" (stored as `ui_mode`, `advanced` = on) is where they start.
Every way back into New goes through `views/new`: "Do more with this" (an earlier job's source, its name and
tracklist; a link is looked up again for its chapters) and, on a failed or cancelled job, "Change and start
again" (the same plus its task and options).
Settings is one column under sticky section tabs and confirms each change next to its control; toasts
(top-right) are for actions and errors.

`store.js` applies job snapshots in request order and replays changes received during each request,
including local removals. Settings saves run in FIFO order, with pending choices layered over the last
confirmed settings; a failed save rolls back only its own patch. The row players give each playback
attempt its own media object so a late error or rejected play cannot stop a newer attempt.

## Life of a job

1. `POST /api/jobs` → `tasks.<task>.validate()` normalises options (bad input → 400 with a readable message).
2. `JobManager.submit()` writes `job.json`, queues it, emits a `job` event.
3. A worker builds a `TaskContext` with the task's stages and calls `tasks.<task>.run(ctx, source)`.
4. The task hands `Progress` scopes to library code. Every call can raise `Cancelled`; child
   processes are registered so cancel kills them immediately.
5. Decisions, warnings and verdicts go to `journal.jsonl` (the job page's **Show details** and **Full log**).
6. The result (files, previews, summary, decisions, metrics) is stored on the job; internal keys
   (`_media`, absolute `path`s) never leave the server. Library preparation hashes the source and stages
   a kept original outside the job lock, checking cancellation between blocks. A per-record digest map
   avoids hashing the same upload twice. Scratch cleanup also precedes the cancellation cutoff.
   Short Library publication and terminal job persistence/events share a manager critical section.
   Nested lock order is Library → manager; do not acquire the Library lock while holding the manager lock.
   Once publication begins, cancellation is refused rather than acknowledged and followed by Done.
   Delete/Clear cannot interleave with terminal persistence. Late callbacks carry their attempt's context
   identity and cannot revive a removed, retried or force-settled job.
7. Quitting the app runs `JobManager.shutdown()`: running jobs are stopped (child processes killed) and
   recorded as "Stopped because AudioLab was closed", queued ones as not started — both can be retried.
   After a crash, the next start marks running jobs as interrupted (their `work/scratch/` goes, unless
   `keep_work_files`) and queued ones as not started. A retry looks a link up again (`source._probe` is dropped).

Subprocess owners call `system.register_process_group()` immediately after spawning with
`own_process_group()`. On POSIX this retains the private group identity even if its leader exits first:
termination waits for the group, then escalates if needed. Only registered groups can be signalled as
groups; repeated teardown is serialized. Windows keeps its existing `taskkill /T` path.

The upload guard counts ASGI request-body bytes before multipart spooling. It raises the parser's
multipart exception on overflow so supported Starlette versions close their temporary files, then
returns `413`. The route must parse the whole form before sending a response.

## Enhance in detail

```
acquire ─▶ decode to a float working copy ─▶ profile (whole-file levels; spectral excerpts beyond 3 min)
        ─▶ classify (metadata + audio features) ─▶ recipe_for_type ─▶ resolve(params, decisions)
        ─▶ chain.render: [AI stage] → HPF → (denoise) → adaptive low-mids + tonal EQ → (HF restore)
                         → (sub-harmonics) → (mono bass) → (glue / leveler) → (de-esser)
                         → loudness → true-peak limiter → dither
        ─▶ previews ─▶ export + file checks (all formats, tags, cover) into "<Title> (Enhanced)/"
```

Light quality skips the AI, tone, bass and dynamics stages; it still removes a DC offset when there is
one, then sets loudness and runs the true-peak limiter. Rules the chain follows:
preservation first (only the `club` recipe uses bold moves by default), the highs are lifted only where
the profile's `top_end` measurement says they are dull (never on a compressed source) and the clarity lift
is dropped on a bright one, the de-esser softens only spikes measured against the track's own typical
S-band share (in Best's stem-aware stage, on the separated vocals only), every automatic choice has a
`why`, quality gates are enforced (true-peak ceiling re-checked; a failed AI reconstruction falls back
to the DSP chain).

`dsp/adaptive.py` replaces the automatic static low-mid cut with relative, time-varying control.
It analyzes every 100 ms against the surrounding 20 s, using channel energy so out-of-phase stereo
does not disappear from detection. Only isolated 0.3–3 s rises in 180–480 Hz qualify, with a 6 dB
relative margin and an audible-level gate. Pinning each event's reference prevents a sustained
change from appearing brief as the moving reference catches up. Duration checks bridge up to two
intervening 100 ms dips, including quiet ones; those dips gain no processing eligibility. This can
conservatively leave closely spaced events alone. A 300 ms gain envelope subtracts
the zero-phase band; maximum reduction is 1.5 dB (3 dB in Strong), with a 0.25 dB modeled whole-band
energy budget. These conservative defaults are engineering hypotheses, not a learned preferred tone.
The control runs before bass boosts/harmonics, skips AI stem EQ, and runs on AI fallback. Volume only
bypasses it. The old static cut remains available only to direct `ChainParams` callers and cannot
stack with the new stage. Bass harmonics are separately opt-in.

`media/verification.py` decodes each complete Enhance export once to measure native-rate loudness
and a 192 kHz true-peak estimate, including buffered tail samples. A ceiling miss or failed check
retains the output and adds a warning; it never silently re-masters a format. Split tracks are not
individually checked. Master and file measurements remain distinct. The compare player uses the
selected export's loudness; master excerpts still use master measurements. Its optional repeat range
uses 40 ms grid points and schedules both sides together, caching at most 8 s for short repeats.
These checks do not qualify sonic preference or every codec implementation.

Dynamic low-mid control, de-essing, compression and limiting collect up to 120 time bins per stage.
Each reports the largest observed reduction and its actual time; a bin is a highlight, not a constant
gain interval. `processing.py` sanitizes these into job and Library history. The comparison's Changes
timeline can repeat a passage around a reported peak. Static EQ, overall gain and unreported stages
are outside this timeline; missing markers do not establish that nothing changed.

Library section previews reuse the kept original and saved resolved parameters. The API clones the
original into a new job input, creates a server-owned preview specification, and submits it to the
existing cancellable Enhance queue. `tasks/preview.py` decodes only the requested 5–30 seconds plus
up to 20 seconds of context on each side, renders through the same chain, then crops aligned excerpts.
The decoder shares comparison playback's preroll and resampling phase. WebM/Matroska alignment can
add short probes at 1, 10 and 25 seconds; if these cannot establish timing, the preview fails with an
actionable explanation instead of decoding an arbitrarily long prefix or silently shifting the passage.
It measures the cropped comparison for volume matching. Context-dependent gain and dynamics can
differ from a full render, so it is explicitly a section estimate. AI stems previews are excluded:
a short reseparation would add latency and different context without establishing faithful results.
Preview jobs write no exports or Library versions. Applying settings creates an ordinary full Enhance
job, reanalyzes the source and checks its actual exports; the preview buffer is never promoted to a master.

Above 600 seconds, `chain.render()` lazily allocates reusable float32 arrays backed by private temporary
files: at most three stereo buffers and three mono limiter controls. Storage is written before mapping
to avoid sparse-file allocation failures on first access. The render closes mappings before deleting
their files, including on cancellation and errors. Filter, compressor, limiter, mix and gain operations
work in blocks with real neighboring context; low-rate analysis/control arrays still grow with duration.
The master is staged and published only after final measurements succeed. Disk mapping reduces large
heap allocations; it does not qualify a RAM minimum or guarantee multi-hour capacity.

Noise reduction stays off unless explicitly chosen; quiet programme levels are not evidence of noise.
Profile and classification share the already measured LRA, and bandwidth uses the profile's existing
Welch spectrum; the classifier's time-resolved spectrum serves a different purpose. Long-file levels
use 10 s reads with overlap for true peak and carried K-weighting state; only scalar energy windows
remain in memory. Loudness, LRA, peaks, clipping, DC and programme percentiles cover the whole file.
Spectrum, stereo, key and classifier features still use 3×30 s excerpts up to 20 min, 5×24 s beyond;
silent excerpts are replaced with a bounded excerpt anchored at sound found by the level scan.
Separate basis fields prevent mistaking these estimates for complete spectral coverage. A successful AI stage transfers its
audio array to the chain rather than retaining a second obsolete buffer through the remaining DSP.

## Extending

- **New recipe**: add a `Recipe` + defaults in `recipes/`, map content types to it in
  `recipe_for_type`, explain each default in `resolve()`.
- **New format**: add a `FormatSpec` in `media/export.py` (ffmpeg codec args) and list it in the task's choices there
  (`_DOWNLOAD_CHOICES`, `_ENHANCE_ORDER`, …); the UI picks the choices up from `/api/health`.
- **New task**: add `tasks/<name>.py` with `validate`, `stages`, `run`; register it in `tasks/__init__.py`;
  add its tile and options in `web/js/views/new/` and its result block in `web/js/views/job/results.js`.
- **New view**: a module in `web/js/views/` that exports `{ el, heading, show, hide }`, a `<section>` in
  `index.html`, `register()` it in `main.js`, and a sidebar link.
- **New AI model**: add it to `ai.MODELS` and the source loader. For the packaged helper, also bundle and verify its offline weights, update the compatible catalog and rebuild the optional asset; it cannot download an unlisted model at runtime.
- **Another OS**: fill in the function's branch in `system.py`.

## Data locations

| What | Where |
|---|---|
| Finished files | `~/Music/AudioLab` (Settings, or `AUDIOLAB_OUTPUT_DIR`) |
| Jobs, settings, tool shims | macOS `~/Library/Application Support/AudioLab`, Linux `~/.local/share/audiolab`, Windows `%LOCALAPPDATA%\AudioLab` (`AUDIOLAB_DATA_DIR`) |
| Library records and kept originals | `<data>/library/` (`originals/` = the downloaded stream or uploaded file, as it came; cleared in Settings → Storage) |
| Log file | macOS `~/Library/Logs/AudioLab`, elsewhere (and with a custom data folder) `<data>/logs` |
| Packaged Core caches/temp | `<data>/cache/yt-dlp`, `<data>/cache/deno`, `<data>/cache/tmp`; configured by `runtime.py` before use |
| Managed packaged AI | `<data>/ai/`: versioned runtime and models, active receipt, cache, temporary files and resumable download; `<data>/.ai-operation.lock` coordinates lifecycle/jobs |
| Native WebKit website data | macOS-managed default Cocoa website store under the app identity; separate from app data (see [Mac cleanup](MACOS.md#uninstall)) |
| Source-mode AI models | Hugging Face cache (`HF_HOME` / `HF_HUB_CACHE`) and/or Torch hub (`~/.cache/torch`, `TORCH_HOME`); can be shared with other software |

Output is user-owned and is never deleted automatically: removing jobs or Library records keeps it, unless
the user opts in to moving a result's files to the system Trash (Remove from Library, Keep only this version). App data also
contains user audio copies: do not treat the whole folder as a disposable cache. Settings → About
opens the actual app data/log folders for optional manual cleanup. The app has no recursive uninstall
endpoint; a chosen output folder can overlap a custom data path, so automatic root deletion would be
unsafe. The Core app's cache setup does not redirect `HOME` or claim to contain all macOS/WebKit data.

## Renaming

"AudioLab" is a **working name**: `audiolab` is already taken on PyPI by an unrelated library, so the
distribution is currently named `audiolab-app` until a final name is chosen. To rename:

1. `audiolab/__init__.py`: `APP_NAME` (window/tab titles, CLI, UI header, app bundle, output folder, and
   the data folder on macOS and Windows — the web UI reads it from `/api/health`) and `APP_SLUG` (the
   Linux data folder and `<SLUG>_` environment-variable prefix).
2. `pyproject.toml`: `name` (must be free on PyPI) and the console-script name under `[project.scripts]`.
3. Launchers and containers: `AudioLab.command`, `start.sh`, `start.bat`, `docker/` (image/container
   names, `AUDIOLAB_*` variables).
   Update the packaged identifier and artifact names in `packaging/macos/` separately; keep the
   identifier stable across releases so macOS can associate native app data with the same app.
4. Docs: README, this file, `docs/API.md` (`X-AudioLab-Token` header, also in `api/security.py` and
   `web/js/api.js`).
5. `grep -rni audiolab audiolab/` for the few remaining literals (some error messages, the
   `AudioLabError` class, the `.audiolab-` temp-file prefix).
6. Optional: rename the import package folder `audiolab/` (a mechanical search-and-replace of `audiolab.`).

Changing `APP_NAME` moves the output folder and the data folder on macOS/Windows; changing `APP_SLUG`
moves it on Linux. Existing users keep their old jobs/settings only if you move the old folder too.
