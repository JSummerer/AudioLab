# Roadmap

Possible next steps after the public beta. These are directions, not a release schedule or
promises. Completed changes belong in [CHANGELOG.md](../CHANGELOG.md).

## Beta priorities
- **Listening checks for "Tone and volume":** compare varied permission-cleared recordings at matched volume,
  including intentional tonal extremes and no-preference outcomes. Generated tests establish numerical
  safeguards, not improved sound.
- **Installation feedback:** make first setup and repair easier using reproducible tester reports.
  Automated regression checks run on macOS for every pull request, and on Windows and Linux when started
  by hand; fresh-machine setup, OS security prompts and native audio/device behavior need broader
  qualification. Windows/Linux remain experimental.
- **Final name:** AudioLab is a working name shared with other audio projects. Review naming and
  distribution availability before a wider release or package-index publication.
- **Better classifier evidence:** evaluate varied, permission-cleared recordings before changing
  content classification thresholds. Generated-signal tests alone cannot establish real-world accuracy.
- **Optional AI:** qualify installation and processing on additional supported hardware and drivers.
- **Hardware requirements:** qualify the exact packaged app plus helper on actual 8 GB and 16 GB Macs
  before publishing a minimum RAM claim. Record model, short-audio workload, whole-app/helper memory,
  GPU/system memory pressure and working disk. Current [hardware recommendations](HARDWARE.md) are
  provisional; future resource checks should account for current memory pressure and model choice.
- **Resource-check accuracy:** distinguish installed RAM from currently available memory in messages;
  include source/output format and output count in disk estimates. Current allowances do not reserve
  memory or disk, so passing preflight is not a guarantee that the job fits.
- **Memory guard:** beyond 10 minutes, Enhance with "Volume only" or "Tone and volume" is now estimated
  at the 10-minute figure plus 0.5 GiB per hour. That slope is calculated from measurements on a 48 GB Apple silicon Mac
  (about three times the measured growth). Confirm it on a small machine and in a container. A data folder
  on tmpfs is the case where the mapped working files are not reclaimable; AudioLab does not detect it yet.
- **Test suite before a production release:** a first pass in November 2026 removed 20 Python and 4 JavaScript
  tests that repeated others (about 675 Python checks remain). A further cut means accepting a risk: the
  release-build checks (`test_packaging_sources.py`, `test_source_seed.py`) and the cover-image hardening
  (`test_thumbnails.py`) are the candidates. Keep the ones that guard user files, cancellation, exports and the
  HTTP contract.

## Bigger features
- **App shortcuts on Windows/Linux**: a Start-menu entry / `.desktop` file, filled in where macOS already has
  one (`system.install_shortcut`, used by `audiolab shortcut`), plus the native window there (`[desktop]` extra).
- **Packaged apps**: a self-contained, signed and notarized Mac app, followed by a Windows installer,
  to reduce setup steps. The current source ZIP uses setup scripts and an internet connection.
- **Long sets on small machines**: qualify whole-app memory and disk use on longer recordings and
  lower-memory machines. Enhance uses disk-backed working buffers, but does not promise constant RAM
  or a maximum supported recording length. A fully streamed mastering chain remains a larger step.
- **Better separation models**: RoFormer / MDX models (e.g. via `audio-separator`) for cleaner vocals,
  plus de-reverb / de-noise models for the voice recipe.
- **Track-aware DJ processing**: snap AI windows and per-track loudness to track boundaries
  (2.1 already *cuts* sets into tracks from chapters/tracklists; detecting boundaries without a tracklist
  is open).
- **Partly finished jobs**: when a job fails late, keep the files that were already complete and mark the job
  "partly finished" (a new job state). Today a failed or cancelled attempt removes everything it saved, so a
  finished MP3 goes with a failed WAV; that stays the rule until then.
- **Folder batch**: pick a whole folder (several files at once already work).
- **Library, later steps** (left out of 2.3 on purpose): "Locate…" for a moved file (needs a native file
  picker in the app window); finding moved files automatically (a relocation scan); retention or a size cap for kept originals
  (today they stay until cleared in Storage); "Fetch the original again" for a link whose original is not kept
  (the entry has `original.url`; download it again, check it is the same recording, attach it to the entry);
  comparing stems of a Split result with each other.
- **Translations** of the UI (at least German).
- **Alternative looks (themes).** The 2.4 design pass mocked two more directions next to the chosen one
  ("Sleeve"): "Inset" (Mac-native grouped panels like System Settings, segmented controls, teal accent) and
  "Console" (one flat aluminium surface; the player as a dark hardware deck with backlit A/B keys and an LCD
  readout). Colours, type, spacing and radii all live in `css/tokens.css`, so a theme is mostly another token set
  plus a few component overrides, picked under Settings → Look.

## Small things
- "Open with…" (Music app, VLC) buttons on results.
- Remember the last few links in the input box.
- Windows: deleting a job while its preview is still streaming can leave files behind (Windows refuses
  to delete open files); retry the cleanup later.
- Key detection: report a second candidate (relative major/minor) when confidence is low.
- Lossy peaks: the −0.1 dBTP lossy limit and the 0.5 dB re-encode margin rest on one real song and two
  synthetic signals; confirm them on more recordings. On those, turning a file down lowered its peaks by up
  to 0.3 dB less than the turn-down for AAC on the real song (0.9 dB on a synthetic signal, and not even
  steadily), 0.2 dB for Opus and never noticeably for MP3. On a dense, hard-limited signal AAC (+3.6 dBTP)
  and Opus (+2.1) need more than the 2 dB bound, so those files keep their warning. Measure with the FFmpeg
  the app ships: Homebrew's FFmpeg 9.0.2 gave AAC and Opus peaks about 0.1 dB apart from the app's 7.1 on
  the same master.
- AAC track starts: on the real song, MP3 and Opus tracks stayed under the lossy limit (at most −0.24 and
  −0.48 dBTP), but AAC tracks cut inside a loud passage peaked up to +2.9 dBTP within 15 ms of their start
  (+2.75 with the older, larger turn-down). Enhance now checks each lossy track and warns when a peak miss
  remains. Save / Convert uses the same cut path. Further work is to give the AAC encoder audio before the
  cut while preserving the exact played boundaries. Verify both playback length and gain across cuts.
- The Library tracks the turn-down of checked lossy tracks. The Library still lacks the full-length file's
  turn-down, and its version line shows the loudness
  target, not the delivered level. An expert choice for lossy peaks is a possible later addition if testers ask.
- Peak-meter gap: on a dense, hard-limited test signal, a 24-bit WAV measured −0.88 dBTP after saving while
  the limiter reported −1.0 (its 4× true-peak meter against the 192 kHz export check). On real music the WAV
  met −1.00 exactly. Align the two meters if real material ever shows a miss.
- Save / Convert output space: MP3, M4A and Opus count only their file on another drive. WAV, FLAC and
  cutting into tracks still need the full working space there; counting them needs the source's sample rate
  and channels, the number of tracks and a size bound for the embedded cover at preflight.
