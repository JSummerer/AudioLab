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
| `native_inputs.py` | Native file and folder selection, scoped selection IDs, and file identity checks for direct job inputs |
| `runtime.py` | Source/frozen-runtime boundary: packaged downloader command, private bundled-tool lookup, certificate trust and app-owned cache/temp setup before helpers start; source mode retains its existing environment |
| `cli.py` | `audiolab [app\|serve\|download\|enhance\|separate\|analyze\|doctor\|update\|install-ai\|shortcut]` |
| `api/` | FastAPI app (`app.py`: `create_app`, lifespan, error format, static UI), `security.py` (local-mode Host/Origin checks, token, upload cap), `state.py` (per-app runtime state: manager, live-event hub, caches, health), `jobs.py` (probe + job routes, files, ZIP, excerpt PCM), `status.py` (health, settings, storage, diagnostics, maintenance: downloader update, update check, AI add-on), `events.py` (SSE), `library.py` (Library list and entries, files, PCM chunks for comparing, removal) |
| `jobs.py` | Job records, FIFO queue, worker threads, cancel/retry, restart recovery, event fan-out, the one bounded `shutdown()`; a `job.json` is trusted only when its id is its folder's name; at start it removes leftovers untouched for an hour (job folders without `job.json` that hold only what a submission leaves, Library staging folders); retention, when on, runs at start and every 6 hours. Delete, Clear finished, retention and the start-up sweep all keep a job folder that holds the output folder or a file the Library knows, decided by folder identity (device and inode), so two spellings of one folder on a case-insensitive disk count as the same. A job whose task has returned its result is settled as finished even when shutdown stops waiting for the Library step |
| `outputs.py` | What one job attempt created in the output folder (files as they get their final name, new folders), kept in memory only; a failure or Cancel while the task runs removes exactly that (a folder also when it holds only file-manager leftovers such as `.DS_Store`), never through a link, never a file that changed since, and never after the task has returned its result. A crash or forced quit leaves the attempt's files in place |
| `library.py` | The Library: one JSON record per finished result (Save, Enhance, Split) in `<data>/library/`, independent of job retention; keeps the acquired original of Enhance jobs for comparing (hard link, or a copy where links are impossible or the source file has another name on disk, e.g. a result or imported file used as the source, so editing that file never changes the kept original; named by its content's SHA-1; setting `keep_originals`); groups versions of one recording (video id, link, or an upload's content); picks the result an entry is compared with (`compare_file_id`, lossless first); removing an entry keeps output files unless the user opts in to the Trash |
| `context.py` | `Progress` (scoped progress, journal notes, cancellation, child-process registry) and `TaskContext` (stages of one job) |
| `tasks/` | The four user-facing tasks (`validate`, `stages`, `run`) and `importing.py`, the `import` task behind Library "Add a file" (`import` is a Python keyword, so `tasks/__init__.py` maps task names to modules); `common.py` acquires sources (reusing a kept original for a link when allowed) and publishes outputs; `preview.py` runs bounded Library section previews through the existing Enhance queue |
| `media/` | `proc.py` (cancellable subprocesses), `ffmpeg.py` (system or bundled ffmpeg, probing incl. embedded tags, `decode_pcm`: 48 kHz chunks on a 1/25 s grid that join like one decode; WebM/Matroska's millisecond timestamps get a seek offset measured once per file), `youtube.py` (yt-dlp + deno, the artist/title rule for links, tracks from chapters/tracklists, downloader update and the source install's self-repair, browser sign-in), `export.py` (format matrix, the file types that can be added, tags, cover art, sample-accurate track cutting; WAV/FLAC/AIFF cuts seek near their start; Enhance files are checked before they get their final name against `peak_limit` (the master's ceiling; for a lossy format `LOSSY_LIMIT_DBTP`, just under full scale), and a lossy one over its limit is re-encoded once with a bounded trim; lossy Enhance tracks start at the trim of their format's full file and are checked individually; `lossy_codec`, the one lossless-codec list, also used by Check and the recipes) |
| `media/ytdlp_update.py` | The packaged app's downloader updates in `<data>/downloader/`: fetches yt-dlp's zipimport build, checks size, checksum and that it starts, and records it; `packaging/macos/entry.py` loads it in downloader mode (`load_downloader`) when it is newer than the built-in copy and imports cleanly, else the built-in copy runs and the failure is recorded |
| `updates.py` | The update check (newest AudioLab release for this build, yt-dlp's newest stable release; on "Check now" or, with `check_updates`, at most once a day, also when the record can't be saved: the last check time is kept in memory too) cached in `<data>/updates.json`, and the GitHub helpers shared with `media/ytdlp_update.py`: fixed HTTPS hosts (redirects included), size limits, plain-language errors |
| `analysis/` | `profile.py` (loudness, true peak, LRA, bandwidth, bands, alerts), `levels.py` (bounded whole-reference level and stereo scan), `tone.py` (how the tonal balance moves, chart data from the same scan), `classify.py` (content type + reasons), `key.py` (musical key estimate) |
| `recipes/` | `Recipe` + `ChainParams`, `resolve()` turns recipe + measurements + user overrides into parameters *with reasons*; `chain.render()` is the single mastering chain; `calibration.py` validates compact full-render state and selects the compatible basis for paired auditions |
| `ai/` | Lazy source torch/demucs detection and windowed separation; `install.py` installs source packages, `managed.py` owns the catalog-pinned packaged runtime lifecycle selected by `api/state.py`, `client.py` invokes the isolated helper, `worker.py` runs/probes it and loads local offline weights |
| `dsp/` | Float32 signal-processing blocks; `scratch.py` reuses disk-backed full-rate buffers for Enhance beyond 10 minutes; `timeline.py` collects bounded reduction highlights without retaining full-rate controls |
| `previews.py` | The Enhance before/after excerpt (a loud 45 s passage, or the whole recording when shorter) at the true relative level, one shared headroom gain; compared when the original isn't kept |
| `processing.py` | Bounded, path-free projection of resolved and applied Enhance processing for results and durable Library history; legacy entries remain readable |
| `settings.py` | Data/output folders (the output folder is only resolved here, never created: whatever writes creates it; it may neither lie inside the data folder nor contain it), env-var limits (`AUDIOLAB_*`), persisted user preferences (checked on load/update; a new snapshot becomes active only after saving succeeds) |
| `resources.py` | Memory/disk/duration preflight, run before any big download: the working folder needs the task's estimate; the output folder needs only the results (never more than the working estimate), is not checked for Check, and needs the full working estimate when the results' size isn't known (Save / Convert other than one MP3, M4A or Opus file of known length: WAV/FLAC keep the source's unknown rate and channels, tracks each embed the cover); also refuses an output folder that can't be written to |
| `diagnostics.py` | Log file setup and the plain-text system/job report for bug reports (`/api/diagnostics`, `audiolab doctor --report`) |
| `errors.py` | `AudioLabError(message, hints, fix, detail)` — every user-visible failure carries plain-language hints, an optional one-click fix and the raw text behind it (`detail`, for bug reports only); `storage_error()` words a full disk or a folder AudioLab may not write to. Anything else unexpected becomes a JSON 500 with hints (`api/app.py`) or "Something went wrong while …" on the job (`jobs.py`) |
| `util.py` | File-name safety, unique paths, hard links, media types, and human-readable sizes and durations |
| `system.py` | **The OS layer** — data/log folders, whether a folder is writable, reveal in Finder/Explorer, notifications, keep-awake, single-instance lock, process groups, memory size, file-name limits, app identity and shortcut. A leaf module with no third-party dependencies |
| `trash.py` | Moving files to the system Trash (only on the user's opt-in, never a permanent delete) and telling links from real folders. OS-specific like `system.py`, kept apart so the AI add-on's pinned `system.py` stays byte-identical |
| `assets/` | App icon |
| `web/` | The UI (see below) |

Resource checks are estimates, not memory reservations. Native-host RAM uses 85% of installed memory,
without subtracting other apps or explicitly budgeting model-specific GPU allocations. Container checks
use the cgroup limit minus its reported usage before applying that fraction. See
[hardware guidance and limits](HARDWARE.md#what-the-app-checks); AI's windowed separation does not make
the complete Enhance chain constant-memory. Disk estimates depend on task/duration/quality, not every
source rate, export format or output count; the added free-space allowance is not reserved capacity.
Long Enhance adds up to 36 bytes per 48 kHz frame (about 5.8 GiB/hour) for disk-backed working buffers.
Beyond 10 minutes, Enhance with "Volume only" or "Tone and volume" is estimated at the 10-minute figure
plus 0.5 GiB per hour: its mapped scratch pages count as reclaimable, and the slope is about three times the growth
measured on one 48 GB Mac. AI stems Enhance keeps the earlier, conservative RAM estimate.

### Native file selection

The desktop launcher opens the UI with a short-lived window secret, passed in the launch fragment and
removed by the UI before routing. The native picker bridge checks that secret and the exact app location
before opening a dialog and checks the location again after selection. The bridge returns names, sizes and
opaque IDs. Existing HTTP routes redeem the IDs on the local instance, for the matching operation and with
the same secret. Only desktop-server pages disallow embedding and child frames. Folder selection adds
immediate supported files by name, up to the same 50-file limit as the New list.

Native New sources keep their canonical path and file identity (device and inode) only in the job's
private source record. Retry and re-run use that original selection in place. If the path is gone or
now resolves to a different file, choose it again. Working data and covers still live in job folders,
and Library originals that came from direct native inputs are copied. Library import retains its existing
ownership and Trash rules.

Locate records a result file's device, inode, size and nanosecond modification time during Library
publication. Under the Library lock, it checks that the former path is unoccupied and the picked file
matches that saved identity, then atomically changes the Library reference. Device and inode are
filesystem-specific, so copied, cross-volume and edited files may need to be added again. This check
uses filesystem metadata rather than reading or hashing the selected audio. Entries without a saved
identity cannot be verified because the grouping key can come from a different file. Located files
outside their recorded output root are omitted from Trash actions, and Library reads check that the
located path still points to the same file. Job records and output ownership roots stay as recorded.

### Platform code
Everything that differs between macOS, Windows and Linux lives in `system.py`; other modules call its
functions and never branch on the platform themselves. The only exceptions are the Windows ffmpeg shim
in `media/ffmpeg.py` (the `.exe` suffix and copying instead of symlinking), and `cli.py`, which prints
the platform in `doctor`. Porting a feature to another OS means filling in its branch in `system.py`.

### Self-contained macOS Core build

The source launchers remain for development and the source-mode AI installer. The downloadable Core app has a separate
entry, `packaging/macos/entry.py`, frozen with the spec and pinned build inputs in `packaging/macos/`.
It starts the same native window/server/task pipeline without `start.sh`, uv, pip or a user's Python.
Its private downloader mode dispatches yt-dlp inside the frozen executable: a newer downloaded update
from `<data>/downloader/` when it passes its checks (`media/ytdlp_update.py`), else the built-in copy.
That executable is not a general-purpose Python interpreter; the spec freezes extra standard-library
modules so an update that starts using one still runs. `runtime.py` chooses this mode and the bundled FFmpeg/Deno.

Without AI, the main app exposes Save/Convert, Check and Enhance with "Volume only" or "Tone and volume".
Since version 2.4.0b2 it offers a managed optional add-on containing a frozen AI helper and both
supported model choices; the main app's bundled Python packages remain immutable. `ai.install_supported`
is driven by a build-generated, bundled catalog, not simply `packaged`. Split/AI stems use the healthy
managed helper when present. Source/CLI/Docker keep their existing optional-AI and update paths.
The packaged app updates its downloader only on a click ("Update downloader"), never by itself after a
failed link; replace the app to update everything else. The update check only reads GitHub's release
lists and links to the release page; it downloads and installs nothing.

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
not overlap the data folder, which holds AI storage; settings and task output creation both enforce this. Archive extraction accepts
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
index.html            the shell: sidebar (collapses to an icon rail, ⌘\ / Ctrl+\; starts collapsed below 1000 px; bottom tab bar on narrow screens) + one <section> per view
css/                  tokens.css (colours, type, spacing; light/dark) · base.css (reset, shell) · components.css (shared controls)
                      · one file per view: new.css · activity.css · job.css · library.css (+ the compare player) · adjust.css · settings.css
                      · eq-editor.css for the graphical EQ
fonts/                the bundled title serif (Source Serif 4, OFL) for browsers and systems without ui-serif; README says how to swap it
js/main.js            boot, global wiring (paste or drop a link anywhere, drop files, deep links ?url=&task= that wait for Load, notice banner)
js/router.js          hash routes #/new · #/activity · #/job/<id> · #/library[/<entry id>] · #/adjust/<entry id> · #/settings[/<section>]
js/api.js store.js    HTTP client + token · app state, event bus, live updates (SSE, polling fallback)
js/dom.js ui.js       tiny DOM helpers and icons · shared controls (toasts, dialogs, menus, meters, segmented…)
js/player.js          job page players: stems, tracks, one file (says when this browser can't play its format)
js/compare.js         the compare player (job page and Library): original | result sample-locked on one Web Audio clock from
                      server-decoded PCM chunks, crossfaded switch, whole-song volume match · plain Library file player
js/actions.js         job actions shared by views (cancel, retry, remove, clear finished…)
js/eq.js              canonical EQ normalization and text/APO parsing/formatting, optional examples, compact Fine-tuning editor
js/eq-editor.js       graphical/numeric/text EQ, drag and keyboard edits, bypass/undo/reset, lightweight draft snapshots
js/eq-response.js     plots server-equivalent zero-phase filter magnitude; no browser audio processing engine
js/adjust-overview.js original level overview with distinct draggable audition and processing-section bounds/fades
js/adjust-audition.js identity-owned paired audition jobs, aligned comparison player and bounded spectrum lifecycle
js/audition-spectrum.js on-demand channel-energy spectrum of exact cropped PCM with declared frequency/gain basis
js/section-eq.js      section EQ: the same checks as the server, the section editor in Fine-tuning
js/tone-chart.js      the tone timeline chart (Check, Enhance, Library previews; a click picks a preview start)
js/views/new/         the "New" view: source (link/files/playlist), names, cover, task + options, tracks, start, and the
                      sleeve beside them (what is being made + the start row; a bottom bar when narrow)
js/views/activity.js  the job list (in progress, then finished by day; filters)
js/views/job/         one job: live progress, failures with fixes, results per task
js/views/library.js   finished results by recording: versions, files, play/compare, kept originals, remove
js/views/adjust.js    optional saved-version workspace, in-app drafts, passage/section editing, comparison and full Apply
js/views/settings.js  preferences, Updates & add-ons (app, downloader, AI add-on, audio engine), storage, about
```

Views stay mounted; the router shows one at a time and calls its `show`/`hide` hooks (a hidden job
page stops its players and polling). There is no global Simple/Advanced mode: each task in New folds its
rarely used choices and fine-tuning under **More options** (folded, it says how many differ from their
defaults, with Reset; the name and cover picture there are never counted or reset), and whatever is set
there is sent with the job. Job pages have a **Show details**
disclosure (numbers, decisions, log details). Both remember open/closed in the browser (`localStorage`; the macOS
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
2. `JobManager.submit()` writes `job.json` (a job whose record can't be written is refused, never queued),
   queues it, emits a `job` event.
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
   Each attempt records what it creates in the output folder (`outputs.py`: `export` notes each file as
   it gets its final name, `tasks/common.py` each new folder). A failure or Cancel while the task runs
   discards exactly that before the outcome is published, so no half result stays and a retry reuses the
   same names; anything else in the folder stays. Once the task has returned its result nothing is
   discarded: a Cancel or quit during the Library step still finishes the job as done, with a warning
   that it was not added to the Library. The record lives in memory only, so after a crash or forced
   quit the attempt's files stay; nothing is removed from the output folder at a later start.
7. Quitting the app runs `JobManager.shutdown()`: running jobs are stopped (child processes killed) and
   recorded as "Stopped because AudioLab was closed", queued ones as not started — both can be retried.
   After a crash, the next start marks running jobs as interrupted (their `work/scratch/` goes, unless
   `keep_work_files`) and queued ones as not started. A retry looks a link up again (`source._probe` is dropped).
   Each start, under the instance lock, also removes job folders without `job.json` (an upload that never
   became a job) and Library staging folders left by a killed publication, once untouched for an hour.
   Only then, holding that lock, does the packaged app delete downloader update archives `active.json` no
   longer names (`AppState.started`); a second launch never touches the running app's files.

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
change from appearing brief as the moving reference catches up. Events touching either file boundary
pass unchanged because the full rise and return are unknown. Duration checks bridge up to two
intervening 100 ms dips, including quiet ones; those dips gain no processing eligibility. This can
conservatively leave closely spaced events alone. A 300 ms gain envelope subtracts
the zero-phase band; maximum reduction is 1.5 dB (3 dB in Strong), with a 0.25 dB modeled whole-band
energy budget. These conservative defaults are engineering hypotheses, not a learned preferred tone.
The control runs before bass boosts/harmonics, skips AI stem EQ, and runs on AI fallback. Volume only
bypasses it. The old static cut remains available only to direct `ChainParams` callers and cannot
stack with the new stage. Bass harmonics are separately opt-in.

`media/verification.py` decodes each complete Enhance export once, before it gets its final name, to
measure native-rate loudness and a 192 kHz true-peak estimate, including buffered tail samples. Lossless
files are checked against the master's ceiling and never re-encoded. A lossy file (MP3, AAC, Opus) is
checked against `export.LOSSY_LIMIT_DBTP` (-0.1 dBTP, or the ceiling when higher): encoding may use the
master's headroom up to there. Above it, the file is encoded once more from the master, turned down by
the excess plus `TRIM_MARGIN_DB` (0.5 dB: AAC's and Opus's peaks don't follow a small turn-down exactly),
and checked again, but only when that turn-down is at most `TRIM_MAX_DB` (2 dB, a bound, not a clamp);
"What we did" names the trim. A remaining miss or failed check retains the output and adds a
warning; it never re-masters a format. Check (`tasks/common.source_format`) uses the same
limit, including the meter's tolerance, for a lossy source. Lossy Enhance track cuts start
with the full file's trim, then run the same verification on each staged cut. Each retry encodes and
decodes only that cut. The 2 dB limit is on the total trim from the master, including the inherited
amount. This adds one decode of the covered track audio, plus a further encode and decode of each
retried track. Existing cut points are used on both attempts. Independent track trim can change gain
at an adjoining boundary. Master and file measurements remain distinct. The compare player uses the
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
existing cancellable Enhance queue. If normalized effective parameters are unchanged and a full-length
export remains, `tasks/preview.py` decodes the requested 5–30 seconds directly from that saved output
(lossless first) and the original. This preserves the full render's processing and includes that export's
codec effects. It reads the saved file where it is; moving it before decoding can fail that attempt.
If parameters change or no export remains, it decodes the passage plus up to 20 seconds of context
on each side, renders through the same chain, then crops aligned excerpts.
The decoder shares comparison playback's preroll and resampling phase. WebM/Matroska alignment can
add short probes at 1, 10 and 25 seconds; if these cannot establish timing, the preview fails with an
actionable explanation instead of decoding an arbitrarily long prefix or silently shifting the passage.
It measures the cropped comparison for volume matching. The result distinguishes `saved_export` from
`section_estimate`: in a new section render, context-dependent gain and dynamics can differ from a full
render. Reusing the old gain after a tonal/dynamics edit would not establish full-render equivalence.
Short previews require a saved render that used no AI processing. A recorded AI fallback retains the
non-AI preview settings and can be adjusted. A full Apply from that version uses Tone and volume again.
Preview jobs write no exports or Library versions. Applying settings creates an ordinary full Enhance
job, reanalyzes the source and checks its actual exports; the preview buffer is never promoted to a master.

The optional routed Adjust view requests paired auditions: saved settings re-rendered and edited settings
use the same decoded original bounds and context. Original is a third reference, and all three crops
share one headroom gain. The existing PCM player performs synchronized switching and LUFS matching on
the audible crop. This re-rendered baseline is distinct from the actual saved export. Compatible full-render
calibration can retain pre-limiter gain, detector gates/thresholds/budgets, Voice leveler threshold and
Volume only's DC/peak basis; upstream edits invalidate dependent detector values on both sides. Edited
EQ remains an estimate because full-program normalization can change even outside an edited section.
Post-limiter correction stays after limiting and is never doubled with a local safety correction; when an
edit leaves a saved correction out of both sides, the pair's basis says so.
Calibration is optional and versioned; old entries remain readable and missing values are not inferred
from rounded presentation metrics. See [the API contract](API.md#library) for units and compatibility.

Adjust keeps small draft settings across in-app navigation and releases its audition player, spectrum
buffers and preview job when superseded or left; the job manager discards finished paired auditions at
startup, since a closed window skips that release. `adjust-audition.js` owns request/cancellation identity
and paired playback; `audition-spectrum.js` computes an on-demand Hann-windowed spectrum of the exact
cropped stereo PCM, averaging channel energy rather than cancelling opposing channels through a mid sum.
It declares its frequency resolution and pre-playback-matching gain basis. New Enhance analyses retain
at most 512 original sample-peak bins for the passage overview, without rescanning the original on open.
The older tone timeline remains a coarse, relative-share observation, with no automatic EQ diagnosis.

Above 600 seconds, `chain.render()` lazily allocates reusable float32 arrays backed by private temporary
files: at most three stereo buffers and three mono limiter controls. Storage is written before mapping
to avoid sparse-file allocation failures on first access. The render closes mappings before deleting
their files, including on cancellation and errors. Filter, compressor, limiter, mix and gain operations
work in blocks with real neighboring context; Section EQ uses the same reusable scratch budget rather
than a whole-section heap copy. Low-rate analysis/control arrays still grow with duration.
The master is staged and published only after final measurements succeed. Disk mapping reduces large
heap allocations; it does not qualify a RAM minimum or guarantee multi-hour capacity.

Noise reduction stays off unless explicitly chosen; quiet programme levels are not evidence of noise.
Profile and classification share the already measured LRA, and bandwidth uses the profile's existing
Welch spectrum; the classifier's time-resolved spectrum serves a different purpose. Long-file levels
use 10 s reads with overlap for true peak and carried K-weighting state; only scalar energy windows
remain in memory. Loudness, LRA, peaks, clipping, DC and programme percentiles cover the whole file.
The same reads accumulate full-reference stereo width and bass correlation. A carried-state 90 Hz
low-pass matches the mono fold's magnitude; 0.5 s windows at 0.25 s hops retain the lowest meaningful
local correlation separately from the global average. Bass below −80 dBFS RMS or −35 dB relative to
the local broadband power does not qualify. Any eligible negative window disables automatic mono bass
for the recording, preserving passages a positive average or excerpts could hide. Explicit On remains
available with an explanation. These reversible thresholds do not claim to detect every short/quiet
phase problem or distinguish intent.
Spectrum, key and classifier features still use 3×30 s excerpts up to 20 min, 5×24 s beyond;
silent excerpts are replaced with a bounded excerpt anchored at sound found by the level scan.
Separate basis fields prevent mistaking these estimates for complete spectral coverage. A successful AI stage transfers its
audio array to the chain rather than retaining a second obsolete buffer through the remaining DSP.

## Decisions and why

- **No automatic tone stabiliser.** A validated detector (30 s windows of seven band shares against the
  recording's median, ≥ 2 dB for ≥ 3 minutes) flagged the tilted first part of one DJ set, but also five
  stretches of a second set and a whole orchestral concert. Band shares cannot tell a recording fault from the
  music changing, so it would mostly correct the music. The tone timeline shows the balance without a verdict,
  section EQ acts only on ranges the user picks, and the chart makes no EQ proposals. Automatic detection would
  need labels: known cues (track boundaries) or listening tests that mark faults.
- **Long-set inconsistencies:** slow drift is no trigger (same evidence). Abrupt steps that persist for minutes,
  a dropped channel or one clipped stretch are measurable and rarely musical: mark them on the timeline first;
  correcting them automatically needs labelled examples of real faults.
- **No learned restoration (e.g. Apollo) for now.** It helps only sources with cut-off highs (MP3, YouTube AAC),
  not the usual full-band YouTube Opus; it runs about real time on Apple GPUs and is unusable on CPU, so a long
  set takes hours; its weights are CC BY-SA 4.0. At most an opt-in AI-pack add-on, offered below an ~18 kHz
  cutoff, replacing only the band above it and called rebuilt, never recovered, and only if blind level-matched
  tests on 128 kbps MP3/AAC clearly beat Brighten muffled highs without harming full-band Opus.
- **Checked and rejected:** a Q correction for the zero-phase bells (they match a DAW bell within 0.006 dB; a
  test pins it); the de-esser off for DJ sets (its tone budget bounds it, no evidence of harm); AAC instead of
  ALAC for AirPods (one lossy step beats two); synthetic stereo (not justified by the evidence).
- **Long recordings' dynamics** use level-independent measures (typical 30 s peak-to-loudness, 3-minute range):
  absolute level and whole-file range misread a quiet, long DJ upload as concert dynamics.
- **Fair comparisons:** louder sounds better at first, so comparisons match volume and flag versions more than
  1 dB apart. ReplayGain tags are later work (WAV can't carry them; Sound Check applies only in Apple Music).
- **YouTube sign-in** only for a paced retry after a refusal; no Premium client (its 256 kbps formats need an
  extra request that can get accounts flagged sooner).
- **Edited auditions stay estimates:** EQ or dynamics edits change whole-program loudness and detector context,
  so reusing the saved gain cannot make a passage equal to Apply.

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
| Packaged downloader updates | `<data>/downloader/`: `active.json`, the active `yt-dlp-<version>.zip`, `failed.json` after a failed start; archives `active.json` no longer names stay while the app runs and are deleted at the next start, once it holds the data folder's lock (`media/ytdlp_update.py`) |
| Last update check | `<data>/updates.json` (`updates.py`); Health only reads it |
| Managed packaged AI | `<data>/ai/`: versioned runtime and models, active receipt, cache, temporary files and resumable download; `<data>/.ai-operation.lock` coordinates lifecycle/jobs |
| Native WebKit website data | macOS-managed default Cocoa website store under the app identity; separate from app data (see [Mac cleanup](MACOS.md#uninstall)) |
| Source-mode AI models | Hugging Face cache (`HF_HOME` / `HF_HUB_CACHE`) and/or Torch hub (`~/.cache/torch`, `TORCH_HOME`); can be shared with other software |

Output is user-owned and is never deleted automatically: removing jobs or Library records keeps it, unless
the user opts in to moving a result's files to the system Trash (Remove from Library, Keep only this version). App data also
contains user audio copies: do not treat the whole folder as a disposable cache. Settings → Help → Maintenance and diagnostics
opens the actual app data/log folders for optional manual cleanup. The app has no recursive uninstall
endpoint; output folders inside the data folder are refused now, but older versions allowed them, so
automatic root deletion would be unsafe. The Core app's cache setup does not redirect `HOME` or claim to contain all macOS/WebKit data.

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
