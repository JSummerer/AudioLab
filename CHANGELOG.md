# Changelog

## 2.4.0 (unreleased)

### Changed
- Public source beta with a clean repository history, contributor and security guidance, and automated
  macOS/Windows/Linux regression checks.
- Public introduction and package description now focus on local audio files and supported links.
  The README has a shorter first-use path, with separate installation and usage guides covering
  updates, safe uninstall, optional AI downloads and explicitly experimental Windows/Linux setup.
- **A new look ("Sleeve")**, calmer and easier to scan, in light and dark: titles in a serif (New York on a Mac),
  one blue for actions and the current choice, lists with thin lines instead of boxes, no monospace or capital
  labels. A finished job opens with its cover and title, then the player and your files; what was done follows.
- **New** shows what you're making beside the form: a sleeve with the cover (an empty sleeve until something is
  loaded), the title, what you get and the start button, always in view; in narrower windows as a bar along the
  bottom. Options read like a sleeve's credits, each label left of its choice.
- Titles look the same in every browser and on Windows and Linux: where the system has no screen serif of its own
  (it does in the Mac app and Safari: New York), AudioLab brings one (Source Serif 4, open license, works offline).
- Simple choices are one row of options with one line about the one you picked and an (i) for details (Save as,
  Type of audio, Quality, Split into, fine-tuning); several choices are a checklist. Fine-tuning shows only
  "Auto" until you turn it off. The four tasks on New are plainer, and the picked one stands out more.
- Folders show short (`~/Music/AudioLab`, the full path when you point at it): the job page, Start, Settings,
  About. Settings → Files: the output folder with "Open folder" and "Change". About: copy buttons for the paths.
- Library: the kept original is one line with "Delete original copy" as a quiet red link, "Remove from Library"
  says what it does, files have labelled Play/Stop buttons, and formats read "WAV 16-bit" instead of "PCM_S16LE".
- Activity: one list with the status next to each job's task; "Try again" keeps its words on narrow windows.
- **One player for comparing** the original with the result, on the job page and in the Library. Both versions
  play on one clock, so switching keeps the exact position (before, they could drift up to about 25 ms apart
  after each play or seek — a stumble on the beat). Every format compares, also Opus originals on macOS 13, and
  "Match volume" uses the whole song's loudness difference. Keys: 1 and 2 switch, Space plays and pauses, and on
  the timeline the arrow keys jump 5 s (Page Up/Down 30 s). Pausing and jumping fade out in a few milliseconds
  instead of clicking. A part that can't be loaded pauses with "Try again", and a start that stays silent goes
  back to Play and says so.
- The job page compares the whole song when its original is kept, starting at a loud passage. Otherwise it plays
  the 45-second excerpt at its real place in the song ("1:52–2:37 of 4:45" instead of "0:00–0:45") and says why
  it is only an excerpt. The Library compares with the lossless result when there is one and says which
  ("Enhanced · WAV 16-bit").
- "Only the changes" is gone: it was turned up by as much as 40 dB without saying so and mostly sounded like
  added noise. Light results say "Volume only, plus a safety limiter on a few peaks" when the limiter touched one.
- Split no longer keeps a copy of the original (nothing compares stems with it, and long uploads took
  gigabytes). Originals kept by earlier Split jobs stay until you delete them.
- **Change and start again** on failed and cancelled jobs: back to New with the same source, task and options,
  to fix what went wrong. When trying the same again can't help, it is the main button.
- "Do more with this" keeps a pasted tracklist and, for links, the chapters, so the next job can still cut the
  recording into tracks.
- Activity: the "Failed" filter is now "Didn't finish" (it also lists cancelled and not started jobs), and
  "Clear finished" asks first.
- Error notices also say what you can try. Settings refuse values they can't use instead of quietly saving
  something else; the output folder must be a full path, for example `~/Music/AudioLab`.
- The macOS app needs macOS 11.3 or newer.
- Noise reduction is now an explicit choice (Off, Light or Strong). Quiet programme levels cannot reliably
  identify background noise, so the voice recipe no longer switches it on automatically or diagnoses noise
  from those levels.

### Fixed
- Comparison playback aligns WebM chunks with the full decode when using the bundled FFmpeg as well
  as newer system builds. Storage totals count hard-linked originals once on Windows. Downloader
  self-repair no longer treats a later job in the same clock tick as a job waiting for an update.
- Launcher recovery explains how to rebuild an unusable Python environment and how to retry a failed
  uv bootstrap. The Windows launcher preserves the application's failure exit status after showing it.
- Removed jobs stay removed even when completion and clearing history happen together. Cancel remains
  available while originals are hashed or copied into the Library; after final publication starts, it
  explains that the result is already being saved instead of promising cancellation and finishing as Done.
  Late work from a stopped attempt cannot change a retried job.
- On macOS/Linux, cancellation also stops a stubborn child process when its parent has already exited.
- Upload size limits also apply to streamed requests without a declared length. A permitted cover image
  no longer consumes the audio file's allowance. Remote cover downloads have size/time bounds and observe
  job cancellation between network operations.
- Activity no longer regresses to an older response or restores a job just removed. Rapid settings changes
  save in order; a failed write leaves active settings unchanged, and damaged saved preferences fall back
  to valid values. Track and stem players return to Play with a useful message if playback fails.
- Ctrl+C stops the remaining CLI playlist and input files. Custom access tokens containing URL punctuation
  work when opening the app.
- Sparse recordings with sound outside the normal analysis excerpts are no longer reported as entirely
  silent. Their confirmation scan can be cancelled.
- Audio analysis reuses measurements it already made. Successful AI enhancement releases an obsolete
  full-length audio buffer before the remaining processing, reducing unnecessary memory retention.
- A kept original could belong to another recording: two uploads with the same name and size (say, a re-bounced
  mix) or two direct file links with the same file name counted as one, so comparing and "Download original"
  played the other file, and deleting it removed it for both. Originals are now told apart by their content.
- An output folder on a drive that isn't connected (or one AudioLab can't write to) broke the whole app. Now a
  notice at the top leads to Settings → Files, jobs stop right away with "The output folder isn't available",
  and "Show in Finder" says when a folder is gone instead of failing.
- macOS app window: dropping a link replaced AudioLab with that web page, with no way back; it now loads into New
  like a pasted link. The window also forgot every remembered choice (collapsed sidebar, More options, Show
  details, the cover switch) each time it opened.
- "Add cover art" off was ignored when converting a file that already has a picture.
- For links, the artist on the card could differ from the one written into the files; both now follow one rule.
- "Try again" on a live stream or premiere that has ended looks the link up again instead of repeating that it is
  still running.
- Playlist items started long after the playlist was looked up lost their title, playlist folder and album tag.
- The job page offered Download and players for files that were moved or deleted; they are marked "Moved or
  deleted", as in the Library. A saved file in a format this Mac can't play (Opus/WebM before macOS 14) shows a
  note with the download instead of a player that never starts.
- Cancelled jobs say why ("Not started — AudioLab was closed…"), and messages name the "Try again" button, not
  "Retry". A cancelled job could stay on "Cancelling…" until the page was reloaded.
- "Do more with this" on a job that was removed meanwhile ended in "Job not found".
- The file picker offered some file types (.mts, .mpg, .wmv, .m4b and others) that were refused after the upload;
  they work now. Files over the upload limit are refused before uploading.
- A full disk or a folder AudioLab can't write to says so, with what to do, instead of "temporary problems often
  go away" or the tool's raw output. Other unexpected errors show a short message and how to report them.
- Check now says when a recording is actually clipped (flat-topped samples), separately from peaks that are only
  close to full scale.
- Download errors: trouble at the site's end says to try again later, a link that needs a sign-in points to the
  sign-in setting, and an unknown error no longer starts an automatic downloader update.
- Hints point to things that exist (Settings → Downloads & sign-in → "Update downloader", "Copy details for a bug
  report", "Copy system report") instead of terminal commands; a Split refused on a slow computer no longer
  suggests "Standard quality".
- A second result of a title with a dot got a garbled folder name ("Song (feat (2). X) (Enhanced)"); it is now
  "Song (feat. X) (Enhanced) (2)".
- Smaller fixes: Enhance cut tracks in the format clicked first instead of the first one shown; an empty artist
  in the name editor didn't remove a file's own artist; Settings → About rounded the length limit to hours ("0 h"
  for 20 minutes); Storage counted an upload's kept original twice; adding an audio file named like
  `cover-original.wav` together with your own cover deleted it; a crashed job left its large temporary files
  behind; adding a long playlist sends far fewer updates; Save without a format (from a script) uses the format
  set in Settings.

## 2.3.0 — Library, covers and tracklists (Oct 2026)

### New
- **Library** (sidebar): every Save, Enhance and Split result finished from this version on, grouped by
  recording with its versions — play, show in Finder, open the job, download, or remove it from the Library
  (your files stay). Moved or deleted files are marked as such. Entries outlive the automatic job cleanup.
- **Full-length compare**: the original against the enhanced version over the whole file, time-aligned —
  switch at any point and keep the position, volume-matched (the louder one is turned down; "Match volume"),
  jump by track/chapter markers or ±10 s. Both files are streamed; no extra audio files are made.
- **Kept originals**: Enhance and Split keep the downloaded stream or uploaded file (as it came — about
  1 MB per minute for links) in AudioLab's own data so the Library can compare. Settings → Storage has
  "Keep originals for comparing", their size and "Delete kept originals"; the Library can drop one recording's.
- **Your own cover image**: pick or drop a JPG/PNG/WebP for a link or a file; it goes into every file that
  can carry one. **Also save the cover as a picture** puts the cover at full size (a link's thumbnail, to
  edit elsewhere) next to the job's files; finished jobs offer *Save cover to folder*.
- **Paste a tracklist** for long sets — for links and now also files: checked as you type, with the
  problems by line and a preview of the tracks it found, then split into tracks.
- **Format info**: an (i) next to every format says what it is, about how big it gets, where it plays and
  its limits.

### Changed
- The global Simple/Advanced switch is gone: Save, Enhance and Split each end with **More options**
  (remembered; shows "2 changed · Reset" when folded), and everything you set is used. Job pages have
  **Show details**; Settings → "Show all options by default" sets the starting point.
- The sidebar can be collapsed to a narrow strip of icons (button next to the logo, or ⌘\); it stays that way.
- Settings is one column with section tabs at the top; a change shows a small "Saved ✓" next to it instead
  of a pop-up. Notices are smaller and sit top right.
- Quitting asks only while a job is actually running (waiting ones are kept as "Not started" for *Retry*).
- Corrected format descriptions: Apple Lossless isn't "for AirPods" (Bluetooth AirPods don't play
  lossless), Opus downloads are re-encoded, and FLAC/WAV made from a link add no quality.

### Fixed
- Enhancing an uploaded file keeps its embedded cover art.
- Seeking in long files in the players (HTTP Range, Starlette ≥ 0.49.1); `.m4a` files are served as
  `audio/mp4`.

## 2.2.0 — a proper app (Oct 2026)
No new processing features — this release is about how AudioLab looks, starts and is built.

### App
- **A real macOS app**: the first start adds *AudioLab* to your Applications folder; from then on it opens
  like any other app — its own window, Dock icon and menu ("Quit AudioLab"), no terminal. Starting it again
  brings the open window to the front. After you replace the folder with a newer version, the app
  reinstalls what changed on its next start.
- **Quitting is safe**: closing the window or ⌘Q asks first while jobs are running, then stops them cleanly
  (no leftover ffmpeg); they can be run again with *Retry*. An AI-features install keeps going after a quit.
- **New layout**: a sidebar with **New**, **Activity** and **Settings** instead of one long page; each job has
  its own page (with a Back button) instead of a side panel; Activity groups jobs by day with filters;
  Settings is a page with sections. On narrow screens the sidebar becomes a bottom tab bar.
- Startup problems of the app window show a message instead of failing silently; the log moved to
  `~/Library/Logs/AudioLab` on macOS.

### Code & repository
- The server is split into small route modules (`audiolab/api/`); everything that differs between macOS,
  Windows and Linux now lives in one module (`system.py`), so porting means filling in one place.
- The web UI is split into views (`web/js/views/`), a tiny router and four stylesheets — still no build step.
- One bounded shutdown path for jobs (cancel, kill child processes, save), also used by Ctrl+C.
- Dead code removed; consistent formatting and linting (ruff), tests run with pytest; a GitHub Actions check
  that runs on demand (no automatic runs while the repo is private); Docker files moved to `docker/`.
- Prototype-era documents removed from the maintained source tree.

## 2.1.0 — hardening & features (Oct 2026)

### New
- **Split into tracks**: YouTube chapters or a timestamped tracklist in the description turn a DJ set or
  album upload into one file per track (Save and Enhance, `--split-tracks`).
- **Light quality — volume only**: one fixed level per type (music −14 LUFS, DJ/club −11, live and voice
  −16) with true-peak safety, without changing the sound. Good for a playlist or a folder of files (`--light`).
  About twice as fast as Standard and needs a third of the memory.
- **Musical key** (estimate, with Camelot code) in Check and Enhance results.
- **YouTube sign-in from your browser** (opt-in, Settings or `--cookies-from-browser`) for age-restricted
  videos and "confirm you're not a bot" blocks.
- Converting a local file keeps its own title/artist/album tags (the file name is only a fallback).
- Check reports the source's real sample rate, channels and codec, and says when it can't judge the
  frequency range (pure tones, silence).
- **Edit title / artist** before saving — used for file names and tags (`--title`, `--artist`).
- **Several files at once**: drop or choose many files — one job each. The CLI takes several inputs too.
- **Download all as ZIP** for results with several files (stems, tracks) — handy on Docker/NAS.
- **"Send to AudioLab" bookmarklet** and deep links (`/?url=…&task=…`); paste a link anywhere in the window.
- **Self-repairing downloader**: when YouTube changes and a download fails, AudioLab updates yt-dlp
  once and retries by itself (Settings → can be turned off). Settings shows when the downloader is old.
- **Bug-report helpers**: "Copy details for a bug report" on failed jobs, "Copy system report" in Settings,
  `audiolab doctor --report`. Home folder paths are shown as `~`, tokens are never included.
- **Storage overview** in Settings (space used by AudioLab's own working data).
- Log file in the data folder (`logs/audiolab.log`, rotating).

### Hardening
- Local mode refuses requests from other websites (CSRF) and DNS-rebinding hosts.
- Ctrl+C / closing the terminal now shuts down within seconds even with a browser tab open, and cancels
  running jobs cleanly (no leftover ffmpeg/downloads). Such jobs say "Stopped because AudioLab was closed".
- Windows: launcher retries an incomplete first install, works without the `py` launcher and from network
  shares; shorter file names (MAX_PATH); retries when another program briefly locks a file; process-tree
  kill is verified; console output never crashes on unusual characters; keeps the PC awake during jobs
  (also on Linux via systemd-inhibit).
- Linux launcher explains a missing `python3-venv` and falls back to uv.
- *Note:* the Windows/Linux items above were written from a code review and have **not yet been run** on
  those systems.
- Robustness sweep with unusual inputs (silence, very short files, 5.1 audio, 8–192 kHz, corrupt files,
  exotic names, NaN samples, white noise, +6 dBFS float files) — friendlier messages instead of crashes;
  white noise is no longer mistaken for speech.
- Memory/disk safety checks recalibrated from measurements (Check of long files, float working copies).
- Faster true-peak limiter (one oversampling per pass), identical output.
- Docker: dependencies are cached in their own layer (code updates rebuild in seconds); the auto-generated
  access token is kept across restarts.

## 2.0.0 — the redesign (Oct 2026)
- The *ClubReady* prototype became AudioLab: one app with four tasks (Save, Enhance, Split, Check),
  a job queue, a new web UI for beginners and experts, a CLI, Docker images and launchers for
  macOS/Windows/Linux.
