# AudioLab HTTP API

The web UI, the desktop window and any script talk to the same local server
(default `http://127.0.0.1:8765`). All endpoints live under `/api`. JSON in, JSON out,
except uploads (multipart) and media/file downloads.

**Auth (optional):** when the server runs with `AUDIOLAB_TOKEN=...`, every `/api` request
(except `GET /api/ping`) needs header `X-AudioLab-Token: <token>` or query `?token=<token>` (use the query form for
`<audio src>`, `<img src>`, `EventSource` and download links). Missing/wrong token → `401`.
Percent-encode the query value; characters such as `+`, `&` and `#` are part of the token.

**Errors:** non-2xx responses carry `{"detail": "human readable message"}`. Failures AudioLab explains (`400`)
also carry `"hints": ["what you can try", …]` and `"fix"` (see Job `error`). Anything unexpected is a `500` with
`{"detail": "Something went wrong in AudioLab.", "hints": [… Settings → About → "Copy system report" …], "fix": null}`
(the traceback goes to the log); a full disk is `500` "The disk is full." and a folder AudioLab may not write to
(permissions, a read-only drive) `500` "AudioLab can't write there.", both with hints.

**Browser safety:** without a token, requests carrying an `Origin` header from another site, or a `Host`
header that is not a loopback name (DNS rebinding), are refused with `403` — other web pages cannot drive a
local AudioLab. With a token these checks are off (the token already blocks both; reverse proxies work).

---

## Objects

### Job
```jsonc
{
  "id": "j_20261005_192501_ab12cd",
  "task": "download",              // download | enhance | separate | analyze
  "status": "running",             // queued | running | done | failed | cancelled
  "title": "Artist - Song",        // best known title (probe → filename → id). A link started without a known
                                   // title shows the link until the job has looked it up; then this changes (a `job` event)
  "source": {                      // what the job works on
    "kind": "url",                 // url | file
    "url": "https://youtu.be/…",   // kind=url
    "name": "song.mp3",            // kind=file (original filename)
    "thumbnail": "https://…",      // optional (url jobs)
    "duration_s": 213.0,           // optional, when known
    "cover_name": "art.png"        // optional: the job has an own cover image (file name as uploaded)
  },
  "options": { … },                // task options after validation (defaults applied; see below)
  "stages": [ {"key": "acquire", "label": "Getting audio"}, … ],
  "stage": "acquire",              // current stage key ('' while queued)
  "progress": 42.5,                // overall 0..100
  "message": "Downloading… 42 % (3.1 MB/s)",
  "queue_position": null,          // 1-based position while queued, else null
  "created_at": 1759690000.0, "started_at": null, "finished_at": null, "updated_at": 1759690001.0,
  "error": null,                   // or {"message": "…", "hints": ["…"], "fix": "update_downloader" | "install_ai" | "use_cookies" | "retry" | null,
                                   //     "detail": "…"}  — `detail` (optional): the raw technical text behind it (a tool's
                                   //     error output, an exception) for bug reports; never shown as the message.
                                   //     A full disk ("The disk is full.") or a folder AudioLab may not write to
                                   //     ("AudioLab can't write there.") has `fix: null`: trying again won't help.
  "result": null                   // Result when status=done (may gain files later, e.g. a saved cover)
}
```

### Result
```jsonc
{
  "output_dir": "/Users/me/Music/AudioLab/Song (Enhanced)",  // folder holding the files, or null
  "files": [
    {"id": "f0", "name": "Song (Enhanced).mp3", "label": "MP3 · 320 kbps", "format": "mp3_320",
     "kind": "audio", "size_bytes": 8123456, "playable": true,      // kind: audio | video | report | cover
     "exists": true},              // checked on every read: false = moved or deleted (its routes answer 410)
    {"id": "f1", "name": "01 - Artist - Track.mp3", "label": "01 · Artist – Track", "format": "mp3_320",
     "kind": "audio", "size_bytes": 9123456, "playable": true, "exists": true,
     "group": "tracks", "track": 1, "start_s": 0.0, "end_s": 312.4},   // split_tracks: one entry per track
    {"id": "f2", "name": "Song - cover.jpg", "label": "Cover image", "format": "jpg",   // save_cover: the cover
     "kind": "cover", "size_bytes": 312456, "playable": false, "exists": true}         // as a picture (jpg | png)
  ],
  "cover_source": "link",          // where the job's cover comes from: own | link | file | null (none) —
                                   // what "Save cover to folder" (POST /api/jobs/{id}/cover) saves (Save,
                                   // Enhance, Split; older results lack the key)
  "library_id": "j_…",             // set when the result was recorded in the Library (= the job id; Save,
                                   // Enhance and Split results with files) — see Library below
  "tracks_dir": "/Users/me/Music/AudioLab/Set (Tracks)",               // split_tracks only, else absent
  "previews": [                                                       // things the UI can play inline
    {"key": "original", "label": "Original"},                         // enhance: the before/after excerpt
    {"key": "enhanced", "label": "Enhanced"}
  ],                                                                  // separate: {"key": "stem:vocals", "label": "Vocals", "file_id": "f0"} …
  "excerpt": {"start_s": 112.0, "duration_s": 45.0},                  // enhance: where the excerpt is in the song
                                                                      // (a loud passage; a 1/25 s step) — see below
  "summary": ["Detected a DJ set — 1 h 58 min with a steady 126 BPM beat.",
              "Made it 2.7 dB louder (−15.2 → −12.5 LUFS) without clipping.", "…"],   // plain language, novice-facing
  "classification": {"type": "dj_set", "label": "DJ set / club mix", "confidence": 0.86,
                     "reasons": ["1 h 58 min long", "steady 126 BPM beat", "title mentions 'Boiler Room'"]},
  "recipe": {"key": "club", "name": "Club / DJ set"},
  "decisions": [                                                      // expert view: what was chosen and why
    {"label": "Loudness target", "value": "−12.5 LUFS", "source": "auto", "why": "club-style dynamics (LRA 5.1 LU)"}
  ],
  "metrics": {                                                        // before/after measurements (enhance)
    "before": {"lufs": -15.2, "true_peak_dbtp": 0.6, "lra": 5.1},
    "after":  {"lufs": -12.5, "true_peak_dbtp": -1.3, "lra": 4.6}
  },
  "analysis": { … },        // full profile (analyze + enhance): lufs, true peak, LRA, bandwidth, bands, alerts…
  "spectrogram": "spectrogram",                                       // media key, or null
  "warnings": ["…"]
}
```

**Enhance excerpts** (`previews` `original`/`enhanced`, media and PCM keys of the same names): the same 45 s
(or the whole recording, when shorter) of the original and the result, 48 kHz WAVs at their **true relative
level** — one shared gain keeps both under −1 dBFS; they are not volume-matched (the player matches them with
`metrics.before/after.lufs`). `excerpt` says where they are in the song. They are what the UI compares when the
original isn't kept. Results from before 2.4 have no `excerpt`; their excerpts were volume-matched to each other,
and they may still list a `difference` preview ("Only the changes", gone in 2.4) and `metrics.difference_db`.

`analysis` keys used by the UI: `integrated_lufs`, `true_peak_dbtp`, `lra`, `duration_s`,
`samplerate`, `channels`, `bandwidth` (`{"cutoff_hz", "is_lossy_cutoff", "verdict"}`), `bpm`,
`key` (`{"name": "A minor", "camelot": "8A", "confidence": 0.62}` or `null` — music only, an estimate),
`is_silent`, `dc_offset`, and the source's own format: `samplerate`, `channels`, `source_audio_codec`,
`source_bitrate_kbps` (the analysis itself runs on a 48 kHz stereo copy),
`alerts` (`[{"level": "ok|info|warning|danger", "title", "message"}]`).
Long recordings are measured from bounded excerpts (`analysis_basis` describes them). If those excerpts
are silent, a cancellable scan checks the file and anchors a new excerpt to detected sound.
Legacy `noise_floor_db` and `snr_db` fields describe the lower programme-level percentile and its spread;
they do not identify background noise or measure signal-to-noise ratio.

---

## Task options (`options` of `POST /api/jobs`)

| task | options |
|---|---|
| `download` | `{"format": "mp3" \| "m4a" \| "flac" \| "wav" \| "opus" \| "original" \| "video", "embed_cover": true, "save_cover": false, "split_tracks": false, "tracklist": "…"}` — for local files this is a conversion (`original`/`video` not allowed). No `format`: the `download_format` setting (for a file `mp3` when that is `original`/`video`) |
| `enhance` | `{"recipe": "auto" \| "club" \| "live" \| "music" \| "voice", "quality": "light" \| "standard" \| "best", "formats": ["mp3_320", "wav16", …], "split_tracks": false, "embed_cover": true, "save_cover": false, "tracklist": "…", "advanced": {…}}` |
| `separate` | `{"mode": "karaoke" \| "stems4", "format": "wav16" \| "wav24" \| "flac24" \| "mp3_320", "model": "auto" \| "htdemucs" \| "htdemucs_ft", "save_cover": false}` |
| `analyze` | `{}` |

`enhance.advanced` (all optional, omitted = auto except noise reduction, which stays off):
`target_lufs` (number), `tp_ceiling_db` (number, −3…−0.1), `bass_db` (0…6), `glue` (`"on"|"off"`),
`hf_restore` (`"on"|"off"`), `denoise` (`"off"|"light"|"strong"`), `mono_bass` (`"on"|"off"`),
`sample_rate` (44100 | 48000 | 96000), `model` (an `ai_models` key, used with `quality: "best"`).
`quality: "best"` uses the AI stage of the recipe (stem-aware EQ for music, voice isolation for speech)
and requires the AI pack.
Noise reduction is explicit-only: choose `denoise: "light"` or `"strong"` when wanted.
The programme-level percentiles cannot reliably distinguish noise from quiet desired content.

`quality: "light"` = **volume only**: one fixed loudness level per detected type (music −14 LUFS, DJ/club
−11, live −16, voice −16) + true-peak safety, no EQ, no compression, no AI — apart from a safety limiter on the
few peaks that would go over the ceiling, the sound stays the same (the summary says whether it touched any).
The limiter never takes more than 3 dB off a peak; if the target needs more, the file stays a bit quieter
(with a warning). Advanced `target_lufs` / `tp_ceiling_db` / `sample_rate` still apply; other knobs are
ignored. Good for evening out a playlist or a folder of files.

**Every task** also accepts `"meta": {"title": "…", "artist": "…"}` — overrides used for file/folder
names and tags (e.g. the user edited the title in the probe card). `"artist": ""` removes the artist;
an empty title is ignored. Ignored when one request creates several jobs (playlist items). Check accepts
`meta` but ignores it: it writes no named output.

**Links (`url`, `urls`):** `url` is one link. `urls` is a JSON list of playlist items (or one link with what its
lookup said, as the UI sends it; at most
`limits.max_playlist_items`), each a link string or `{"url": "…", "title": "…", "duration_s": 201}` (`title` and
`duration_s` optional), plus the optional form field `playlist_title`. A link looked up with `POST /api/probe` in the
last 15 minutes takes its title, length and playlist from that lookup; after that the server only knows what the
request says — so send each item's `title` and `duration_s` and the playlist's `title` as `playlist_title` (the
playlist folder and album tag). A title is one line of at most 200 characters; unusable values are ignored. A link
job without any known title is named after the link until it has looked the link up (see Job `title`). Anything
else than such a list → `400` "urls must be a JSON list of links.".

**Cover image** (download, enhance): `embed_cover` (default: the `embed_cover` setting) puts the link's thumbnail
(cropped square, ≤ 1000 px) — or, for an uploaded file, the picture it already carries — into every file whose
format can hold one (`formats[].cover` in Health; WAV and Opus can't). An **own cover image** (multipart field
`cover` of `POST /api/jobs`) is always used, whatever `embed_cover` says: JPG, PNG or WebP, at most 15 MB and
40 megapixels, checked from the file's header to really be such a picture before anything decodes it (else `400`:
"This file is not a JPG, PNG or WebP picture…" / "This picture is too large (… × … pixels)."), stored as a JPEG of at most 1400 px (longer side, never enlarged) in the
job's folder, next to the picture as uploaded (for `save_cover`) — Retry and re-runs (`source_job`) keep both; a new
`cover` replaces them. Only for `download` and
`enhance` (other tasks → `400`); ignored when one request creates several jobs (playlist items). A job with an own
cover shows `source.cover_name`. Untouched downloads (`original`, `video`) get no cover (their tracks do).
`embed_cover: false` (and no own image) means no picture at all — a converted file's own picture isn't copied either.

**`save_cover: true`** (download, enhance, separate; default `false`): once the outputs are written, the job also
saves its cover as a picture file in its output folder, named like the other outputs — `"<Title> - cover.jpg"`
(`.png` when the picture is a PNG; never replacing a file: `"… (2).jpg"`) — at full size, not the 1400 px copy that
gets embedded: the own cover image as uploaded (JPEG/PNG as they are, WebP → JPEG), else the link's thumbnail (as the
site delivers it, YouTube's WebP → a high-quality JPEG), else the picture an uploaded file carries. Independent of
`embed_cover`. It is a normal result file (`kind: "cover"`): listed with the files, in the ZIP, in the Library entry.
No cover, or one that can't be fetched, is a note in the job's journal — never a failure. For a finished job the same
file can be added afterwards with `POST /api/jobs/{id}/cover`.
Remote thumbnails have a 15 MiB transfer cap and a 30 s elapsed-time budget, checked between reads,
with a 20 s socket timeout. Job cancellation is checked between operations and propagates normally;
an already blocking network operation can delay it.

**`tracklist`** (download, enhance; optional text): a tracklist the user pasted — one track per line, its start
time anywhere on the line (see `POST /api/tracklist` for the rules). It replaces the link's chapters / description
tracklist, works for uploaded files too, and is used for `split_tracks` and as the markers of the Library entry.
Lines the parser can't use → `400` naming them (`"The tracklist has a problem — line 7: time goes backwards (…)."`).
For links the end is checked against the probe's length already; for files only when the job runs (the job then
fails with `"The tracklist doesn't fit this recording — line 9: 1:02:00 is after the end (58:10)."`). Max 50 000
characters; `""` = none. The normalised text is kept in the job's `options.tracklist`.

**`split_tracks: true`** (download, enhance): cut the result into one file per track, using the pasted
`tracklist`, else the probe's `tracks` (YouTube chapters, or a timestamped tracklist in the description) —
so for uploaded files only together with a `tracklist` (download drops `split_tracks` for a file without one).
The full file is still saved. Tracks are named `"NN - Track title.ext"` and tagged title/artist (from "Artist - Track"), album = the
set's title, album artist and track `n/total`; cuts are sample-accurate and the last track runs to the end.
- download: tracks go to `"<Title> (Tracks)"` next to the full file, in the chosen format (`original`/`video`:
  the audio format closest to the source stream — Opus or AAC — since cutting needs re-encoding).
- enhance: tracks are cut from the finished master, in the first selected format, into a `Tracks`
  subfolder of the `"<Title> (Enhanced)"` folder.

When the source has no usable tracks the job still succeeds with the note "No tracklist found — saved as one file."
When a pasted tracklist starts after 0:00, nothing is invented for the part before it: that audio is in no track
file (only in the full file), and the summary says so ("The first 2:00 before track 1 are only in the full file.").

Valid format keys and labels come from `GET /api/health` → `formats`.

---

## Endpoints

`POST /api/jobs` enforces the actual request byte count while receiving, including chunked requests.
The total allowance is `limits.max_upload_mb` MiB of audio + 15 MiB for a cover + 1 MiB for multipart fields
and framing; exceeding it returns `413`. A declared oversized `Content-Length` is rejected immediately.
The audio and cover limits are also checked separately after parsing; the cover allowance cannot enlarge
the permitted audio file. Extra or repeated parts count toward the total.

Settings updates publish active values only after the file is saved successfully. A failed write returns
`500` and keeps the previous active values. Each update merges with the latest saved preferences.
The UI queues its saves in order and keeps later pending choices visible if an earlier save fails.
On startup, malformed saved settings fall back to defaults field by field where possible, without
automatically replacing the saved file.

| Method & path | Body / query | Response |
|---|---|---|
| `GET /api/ping` | – | `{"ok": true}` — liveness check, never needs a token |
| `GET /api/health` | – | Health (below) |
| `GET /api/settings` | – | Settings |
| `PUT /api/settings` | partial Settings JSON | Settings. `400` with detail (nothing is changed) for an invalid value: `output_dir` not a full path ("Enter a full folder path, for example ~/Music/AudioLab.") or not writable, a switch that isn't `true`/`false` (also `"true"`/`"false"`, `1`/`0`), a format key not in Health `formats`, an empty `enhance_formats`, another fixed choice that isn't listed below. Unknown keys are ignored. Works while the current output folder is unreachable |
| `POST /api/probe` | `{"url": "…"}` | Probe (below). `400` with detail for bad links |
| `POST /api/tracklist` | `{"text": "…", "duration_s": 3490}` (`duration_s` optional; alias `duration`) | Tracklist preview (below) |
| `POST /api/jobs` | multipart: `task`, `options` (JSON string), and one of `url`, `urls` (playlist items, see below), `file` (an extension from Health `upload_extensions`, else `400`), `source_job` (id of an earlier job — reuse its source and own cover, e.g. "re-run as Concert" or "now split it"; a link is looked up again when it runs; `404` unknown job, `410` its file is gone → upload again); optional `cover` (image file, see "Cover image" above; `413` over 15 MB) | `{"jobs": [Job, …]}` |
| `GET /api/jobs` | – | `{"jobs": [Job, …]}` newest first |
| `GET /api/jobs/{id}` | – | Job |
| `GET /api/jobs/{id}/journal` | – | `{"events": [{"ts", "kind", "stage", "message", "detail"}]}` |
| `POST /api/jobs/{id}/cancel` | – | Job. Cancellation includes Library hashing/copying; once final publication begins or the job is done, `400` explains that the result is already being saved or has finished |
| `POST /api/jobs/{id}/retry` | – | Job (same id, back in the queue). A link is looked up again (a live stream or premiere may have ended) |
| `DELETE /api/jobs/{id}` | – | `{"deleted": id}` — removes the job and its work files, never the output files |
| `DELETE /api/jobs` | `?scope=finished` | `{"deleted": [ids]}` — clears done/failed/cancelled jobs |
| `GET /api/jobs/{id}/files/{file_id}` | – | file download (attachment); `410` when it was moved or deleted (`exists: false`) |
| `GET /api/jobs/{id}/zip` | – | all output files of a finished job as one ZIP (attachment, streamed) |
| `GET /api/jobs/{id}/media/{key}` | – | inline stream with Range support. Keys: `original`, `enhanced` (Enhance excerpts), `stem:<name>`, `file:<file_id>`, `spectrogram` (and `difference` for results from before 2.4) |
| `GET /api/jobs/{id}/pcm/{key}` | `?offset=<frame>&frames=<n>` | PCM chunk (below) of an Enhance excerpt: `key` `original` or `enhanced`. `404` unknown job or no such excerpt, `400` off the grid |
| `POST /api/jobs/{id}/cover` | – | "Save cover to folder": writes the finished job's cover into its output folder, exactly as `save_cover` does, adds it to `result.files` (saved, and a `job` event) and to its Library entry; returns that file (a Result.files entry). Idempotent: a cover file that is still there is returned as it is (one that was moved or deleted is written again, same id). `404` unknown job, or no output folder (not finished, or the folder is gone); `409` "This recording has no cover image to save."; fetching the thumbnail failed → `400` with detail |
| `GET /api/library` | – | `{"groups": [LibraryGroup, …], "count": n}` — see Library below |
| `GET /api/library/{id}` | – | one LibraryEntry (with `job_exists`). `404` unknown |
| `GET /api/library/{id}/pcm/{file_id}` | `?offset=<frame>&frames=<n>` | PCM chunk (below) of one file of the entry (`original` or a result). `404` unknown entry/file, `410` moved or deleted, `400` off the grid |
| `GET /api/library/{id}/files/{file_id}` | `?download=1` optional | one file of a Library entry (`file_id` from its `files`, incl. `original`): inline with Range support for the players, or as an attachment with `download=1`. `404` unknown entry/file, `410` moved or deleted |
| `POST /api/library/{id}/reveal` | – | opens the entry's output folder (only when `health.local`): `{"ok": true}`, or `{"ok": true, "fallback": "output"}` when that folder is gone and the output root was opened instead (say so). Neither there → `400` "This folder isn't there any more: <path>." with hints. Never creates a folder. `404` unknown |
| `DELETE /api/library/{id}` | – | `{"deleted": id, "original_removed": bool, "freed_bytes": b}` — forgets the entry only: output files are never touched; its kept original is deleted with it unless another entry still uses that file. `404` unknown |
| `DELETE /api/library/{id}/original` | – | `{"id": id, "original_removed": bool, "freed_bytes": b}` — stops keeping the original of this entry's recording: **every entry of the same `group`** gets `original.kept: false` and their kept files are deleted (usually one; a file an entry of another recording still uses stays); the entries and their output files stay. `original_removed`: the recording had a kept original. Nothing kept → `false`/`0`. `404` unknown entry |
| `DELETE /api/library/originals` | – | `{"deleted": n, "freed_bytes": b}` — "Delete kept originals" (Storage): deletes every kept original (only AudioLab's own files in that folder); the entries stay and are detached (`original.kept: false`, also if the same recording is kept again later) |
| `POST /api/jobs/{id}/reveal` | – | opens the job's output folder (only when `health.local`): `{"ok": true}`; when it was moved or deleted, the output root instead: `{"ok": true, "fallback": "output"}` (say so); a job without a folder opens the output root (`{"ok": true}`). Neither there → `400` "This folder isn't there any more: <path>." with hints. Never creates a folder |
| `POST /api/reveal-output` | – | `{"ok": true}` opens the output root folder — made first when it doesn't exist yet (before the first result). It can't be made (`health.output_dir_ok` false, e.g. a drive that isn't connected) → `400` "The output folder isn't available: …" with hints |
| `POST /api/system/update-downloader` | – | `{"ok", "before", "after", "message"}` (takes ~10–60 s) |
| `POST /api/system/install-ai` | – | `{"ok": true}` starts the AI pack install; watch `system` events / health |
| `GET /api/storage` | – | `{"jobs_bytes", "job_count", "finished_bytes", "finished_count", "models_bytes", "library_count", "originals_bytes", "originals_count"}` — space used by AudioLab's own data (not your output files). `originals_*`: the kept originals (clear them with `DELETE /api/library/originals`). Disk space is counted once: an upload's kept original is a hard link to its job's input file, so it counts in `originals_bytes` only, and `freed_bytes` (here and in the Library deletes) counts only files whose last link went — deleting a kept original frees nothing while its job still holds the upload |
| `GET /api/diagnostics` | `?job=<id>` optional | `text/plain` report for bug reports: versions, platform, tools, limits, settings (except the output path, shown separately), recent log lines (+ that job's journal). No tokens or file contents |
| `GET /api/events` | – | Server-Sent Events (below) |

**Serving files:** every file route sends the media type players expect (`.m4a` → `audio/mp4`, `.flac` →
`audio/flac`, `.wav` → `audio/wav`, `.opus` → `audio/ogg`, `.webm` → `audio/webm`) and answers `Range` requests
with `206 Partial Content` (inline and download routes alike), so long files can be seeked without loading them.

**PCM chunks** (`…/pcm/…`, for the compare player): `frames` frames of the file from frame `offset`, decoded to
**48 kHz stereo float32**, little-endian, interleaved (L R L R…), `application/octet-stream`, `Cache-Control:
no-store`. `offset` and `frames` must be multiples of **1920** (1/25 s) and `frames` at most 960 000 (20 s),
else `400`. On that grid the 48 kHz output and every common source rate meet on whole samples, so consecutive
chunks join exactly as one decode of the whole file would (bit for bit for WAV, FLAC, MP3 and WebM/Opus; AAC's
noise-filling bands are random by design; a Matroska file with a non-48 kHz codec may keep a sub-millisecond
offset after a seek). The player plays them back to back, both versions started at the same time on one clock. Fewer frames come back only at the end of the file (none past it). Mono is spread to
both channels and more channels are mixed down, as for the analysis. A file that can't be decoded → `400` with
detail.

### Health
```jsonc
{
  "app": "AudioLab", "version": "2.3.0", "local": true,
  "ffmpeg": {"ok": true, "version": "7.1", "source": "system" | "bundled", "error": null},
  "downloader": {"ok": true, "version": "2026.08.19", "age_days": 47},   // age from the version date
  "ai": {"available": false, "device": null, "reason": "AI pack not installed", "installing": false,
         "install_log": "…last lines…"},
  "output_dir": "/Users/me/Music/AudioLab", "output_dir_ok": true, "data_dir": "…",
  "upload_extensions": [".3gp", ".aac", ".ac3", "…", ".wv"],   // sorted, with the dot: what `file` accepts
  "limits": {"max_upload_mb": 4096, "max_duration_s": 28800, "max_playlist_items": 200},
  "recipes": [{"key": "club", "name": "Club / DJ set", "short": "Punchy, loud, tight bass", "description": "…"}],
  "formats": {
    "download": [{"key": "mp3", "label": "MP3", "description": "Plays everywhere (320 kbps).", "kind": "lossy",
                  "facts": ["Converted to MP3 at 320 kbps.", "About 2.4 MB per minute.", "…"], "cover": true}],
    "enhance":  [{"key": "mp3_320", "label": "MP3 · 320 kbps", "description": "…", "kind": "lossy", "ext": ".mp3",
                  "facts": ["…"], "cover": true}],
    "separate": [{"key": "wav16", "label": "WAV · 16-bit", "description": "…", "kind": "lossless", "ext": ".wav",
                  "facts": ["…"], "cover": false}]
  },
  "format_notes": {"source": "YouTube audio is already compressed (about 130–160 kbps, 256 kbps with Premium): …"},
  "ai_models": [{"key": "htdemucs", "label": "Standard", "description": "…"}]
}
```
`formats[].kind`: `lossy | lossless` (download also `original | video`). `formats[].facts`: 2–4 short plain-language
lines for a format card — what it is, about how many MB per minute, where it plays, limits (cover support is the separate `cover` flag, so the UI words it);
the server is the one source of this text. `formats[].cover`: the files can carry a cover image (WAV and Opus can't;
`original`/`video` downloads get none added). `format_notes.source`: one line about what a link's audio really is,
for under the format picker.
`upload_extensions`: the file types `POST /api/jobs` accepts as `file` (audio and video containers ffmpeg reads) —
the UI's file picker and drop check use this one list.
`output_dir_ok`: the output folder is there and writable, or can be made. `false` (e.g. on a drive that isn't
connected): show a notice pointing to Settings → Files. Settings still load and save; jobs that write files fail
before they start ("The output folder isn't available: …"); Check still works.

### Settings
`{"output_dir", "download_format", "enhance_formats": [..], "separate_format", "ui_mode": "simple|advanced",
"theme": "auto|light|dark", "keep_work_files": false, "embed_cover": true, "keep_originals": true,
"auto_update_downloader": true,
"cookies_browser": "" | "firefox" | "chrome" | "edge" | "brave" | "safari" | "chromium" | "opera" | "vivaldi",
"locked": ["output_dir"]}`
(`ui_mode`: the UI's "Show all options by default" — `advanced` = "More options" open and job details shown until
the user opens or closes them. Name kept for compatibility.)
(`keep_originals`: "Keep originals for comparing" — Enhance results keep their original in the Library
(see Library); off = new results keep none, their entry has `original.kept: false`; already kept ones stay.
Split results no longer keep one (those kept before 2.4 stay until deleted).)
(`output_dir`: a full path (`~` is expanded); `""` = the default folder. The folder is not created on reading;
`GET /api/settings` reports it even when it isn't reachable.)
(`cookies_browser`: opt-in — downloads use that browser's YouTube sign-in, for age-restricted videos and
"confirm you're not a bot" blocks. Only offered when `health.local`; Firefox works most reliably.
Errors that need it carry `"fix": "use_cookies"`.)
(`locked` lists keys pinned by environment variables — show them read-only.)

### Probe
```jsonc
// single video
{"kind": "single", "url": "…", "title": "…", "artist": "…", "uploader": "…", "duration_s": 7080,
 "thumbnail": "…", "is_live": false,
 "tracks": [{"n": 1, "title": "Artist – Track", "start_s": 0.0, "end_s": 312.4}],   // [] when none
 "tracks_source": "chapters",                                    // "chapters" | "description" | null
 "guess": {"type": "dj_set", "label": "DJ set / club mix", "confidence": 0.7, "reasons": ["…"]}}
// playlist
{"kind": "playlist", "url": "…", "title": "…", "uploader": "…", "count": 24, "truncated": false,
 "entries": [{"url": "…", "title": "…", "duration_s": 201}]}
```
The `guess` is metadata-only (title, duration, tags); the job re-checks with real audio analysis.
`artist` follows the rule the job uses for its file names and tags, so the card and the files agree: the site's
music data (artist + track) only when the video is that one track (its title names the track — e.g. YouTube's
"Topic" uploads; several artists are joined with ", "), not a set or mix whose music section lists a licensed song;
else the "Artist - Title" form of the title; else the uploader (without " - Topic"/"VEVO"); `null` when none.

### Tracklist preview (`POST /api/tracklist`)
```jsonc
{"tracks": [{"n": 1, "start_s": 0.0,   "end_s": 330.0, "title": "Opening", "artist": "Artist A"},
            {"n": 2, "start_s": 330.0, "end_s": null,  "title": "Second",  "artist": null}],   // end_s null: length unknown
 "errors": [{"line": 4, "message": "time goes backwards (07:00 comes after 10:00 on line 3)"}]}
```
The same strict parser the jobs use; nothing is guessed. One track per line, the start time anywhere on the line:
`0:00 Artist - Title`, `[00:00:00] Title`, `1. 03:15 Title`, `Title 03:15`, `03:15 - 05:20 Title` (a range: the
first time counts). Times: `M:SS`, `MM:SS`, `H:MM:SS`, also minutes past the hour (`75:30`). Blank lines are
skipped; every other line must give a usable time — the problems come back per line (1-based, as in a text
editor), and `tracks` holds the good lines meanwhile. `message`s (shown after "line N: "): `no time found (expected
e.g. "12:30 Artist - Title")`, `<time> is not a valid time`, `time goes backwards (<time> comes after <time> on
line N)`, `<time> is the same time as line N`, `<time> is after the end (58:10)` / `is at the very end (…)` (only
with `duration_s`). One line is a valid list (one track). Starts are never moved: a first time after 0:00 stays,
the audio before it belongs to no track. Each track ends where the next starts, the last at `duration_s`.
`title`/`artist`: what the track's tags will say ("Artist - Title" is split; `artist: null` → the set's artist);
the file is named `"NN - <the line's title>.ext"`. A job only starts when `errors` is empty. Text over 50 000
characters → `400`.

### Library
Every job that finishes with files (Save, Enhance, Split — not Check) is recorded in the Library, which outlives
the job (jobs expire after `AUDIOLAB_RETENTION_DAYS`, "Clear finished" removes them; the Library keeps its
entries until they are removed with `DELETE /api/library/{id}`). Runs from the command line are not recorded.
An entry is the original plus the final output files — nothing else is written for it (no excerpts: comparing
decodes the full files in chunks, `…/pcm/…`). For Enhance the acquired original is kept as it came — the
downloaded stream (e.g. Opus in `.webm`) or the uploaded file, never a decoded WAV — only in AudioLab's data
folder (`<data>/library/originals/`, never next to the user's files), unless the setting `keep_originals` is off.
A kept file is named by its content, so results with the same original share one, and a different original
(a re-bounced mix of the same name and size, a re-encoded stream) is kept on its own. It goes with its last entry
(`DELETE /api/library/{id}`), for the whole recording (`DELETE /api/library/{id}/original`) or all at once
(`DELETE /api/library/originals`); Storage shows their total (`originals_bytes`).
```jsonc
// LibraryGroup: versions of one recording (same video id / link, or same uploaded content), newest first
{"key": "youtube:dQw4w9WgXcQ",      // or "url:<link>" (also direct file links), "file:<sha1 of the upload>"
                                    // (before 2.4: "file:<name>:<size>")
 "title": "Song", "artist": "Artist", "source": {…},  // from the newest entry
 "latest_at": 1759740000.0,
 "entries": [LibraryEntry, …]}      // newest first

// LibraryEntry: one finished result
{"id": "j_20261006_101500_ab12cd",  // = the job id (stable; the job itself may be gone)
 "job_id": "j_20261006_101500_ab12cd", "job_exists": true,   // false once the job expired or was removed
 "task": "enhance",                 // download | enhance | separate
 "created_at": 1759740000.0,
 "title": "Song", "artist": "Artist",                        // as used for names and tags
 "source": {"kind": "url", "url": "…", "title": "…", "thumbnail": "…", "duration_s": 213.0},  // as Job.source
 "group": "youtube:dQw4w9WgXcQ",
 "duration_s": 213.0, "headline": "Enhanced as Club / DJ set",
 "output_dir": "/Users/me/Music/AudioLab/Song (Enhanced)",   // or null
 "loudness": {"original_lufs": -15.2, "result_lufs": -12.5}, // measured (Enhance), else null
 "markers": [{"n": 1, "title": "Artist – Track", "start_s": 0.0, "end_s": 312.4}],  // tracks/chapters, [] if none
 "markers_source": "chapters",      // chapters | description | pasted | null
 "original": {"kept": true, "size_bytes": 3456789, "format": "webm", "codec": "opus",  // size/format/codec: null
              "url": "https://youtu.be/…"},  // when not kept · url: the link (null for uploads), to fetch it again
 "compare_file_id": "f1",           // the result the kept original is compared with: the first lossless one that
                                    // still exists (WAV/FLAC/ALAC), else the first one; null without a kept original
                                    // (Save, Split, not kept, dropped) or when no result file is left
 "files": [
   {"id": "f0", "name": "Song (Enhanced).mp3", "path": "Song (Enhanced)/Song (Enhanced).mp3",  // relative to the
    "kind": "enhanced", "format": "mp3_320", "label": "MP3 · 320 kbps", "size_bytes": 8123456,  // output folder
    "exists": true, "playable": true, "mime": "audio/mpeg"},
   {"id": "f2", "kind": "track", "track": 1, "start_s": 0.0, "end_s": 312.4, …},
   {"id": "f0", "kind": "stem", "stem": "vocals", …},                 // Split results
   {"id": "original", "name": "Song.webm", "kind": "original", "format": "webm", "codec": "opus",
    "label": "Original (as downloaded)", "size_bytes": 3456789, "exists": true, "playable": true,
    "mime": "audio/webm"}                                              // only while original.kept
 ]}
```
`files[].kind`: `saved` (Save) · `enhanced` · `stem` · `track` (split into tracks) · `cover` (the cover saved as a
picture: `save_cover` or `POST /api/jobs/{id}/cover`; `playable: false`) · `original` (the kept
original; `label` is "Original file" for uploads; listed only while `original.kept` is true). `original.kept` is
false for Save results and new Split results (Split entries from 2.3 may still have one), when `keep_originals` was off, and after the original was dropped or cleared. `exists` is checked on every `GET /api/library`: a file the user
moved or deleted stays listed with `exists: false` (its file route answers `410`). `playable`: a format browsers
can usually play (for the plain players; comparing decodes on the server, so any format compares) — WebM/Opus
needs macOS 14 / Safari 17 in the app window, so probe with `canPlayType(mime + '; codecs=' + codec)` and handle
playback errors. `path` is relative to the output folder of
that time (absolute for a file outside it).

### Events (`GET /api/events`)
```
event: job
data: <Job>                      on every change (throttled to a few per second per job)

event: removed
data: {"ids": ["j_…"]}

event: system
data: {"ai": {…}, "downloader": {…}}   when an install/update changes health

: ping                           every 15 s
```
Clients should also refetch `GET /api/jobs` after a reconnect. When the app quits, open streams end
and new ones get `503` until the server is gone.

## Deep links

The UI understands `/?url=<link>` (loads the link, as if pasted) and `&task=download|enhance|separate|analyze`
(pre-selects the task). Settings offers a **"Send to AudioLab" bookmarklet** built on this.
