# Roadmap

Possible next steps after the public source beta. These are directions, not a release schedule or
promises. Done items move to [CHANGELOG.md](../CHANGELOG.md).

## Beta priorities
- **Installation feedback:** make first setup and repair easier using reproducible tester reports.
  Automated regression checks cover macOS, Windows and Linux; fresh-machine setup, OS security prompts
  and native audio/device behavior need broader qualification. Windows/Linux remain experimental.
- **Final name:** AudioLab is a working name shared with other audio projects. Review naming and
  distribution availability before a wider release or package-index publication.
- **Better classifier evidence:** evaluate varied, permission-cleared recordings before changing
  content classification thresholds. Generated-signal tests alone cannot establish real-world accuracy.
- **Optional AI:** qualify installation and processing on additional supported hardware and drivers.

## Bigger features
- **App shortcuts on Windows/Linux**: a Start-menu entry / `.desktop` file, filled in where macOS already has
  one (`system.install_shortcut`, used by `audiolab shortcut`), plus the native window there (`[desktop]` extra).
- **Packaged apps**: a self-contained, signed and notarized Mac app, followed by a Windows installer,
  to reduce setup steps. The current source ZIP uses setup scripts and an internet connection.
- **Long sets on small machines**: disk-streamed two-pass mastering chain (O(block) RAM) so 3 h sets fit
  a 16 GB laptop or a 4 GB container.
- **Better separation models**: RoFormer / MDX models (e.g. via `audio-separator`) for cleaner vocals,
  plus de-reverb / de-noise models for the voice recipe.
- **Track-aware DJ processing**: snap AI windows and per-track loudness to track boundaries
  (2.1 already *cuts* sets into tracks from chapters/tracklists; detecting boundaries without a tracklist
  is open).
- **Folder batch**: pick a whole folder (several files at once already work).
- **Measure exported files**: verify true peak of encoded MP3/AAC files, not only the master.
- **Library, later steps** (left out of 2.3 on purpose): "Locate…" for a moved file (needs a native file
  picker in the app window); "Move to Trash" for a result's files (explicit, separate from "Remove from
  Library"); finding moved files automatically (a relocation scan); retention or a size cap for kept originals
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
- Waveform view in the compare player with a loop region.
- "Open with…" (Music app, VLC) buttons on results.
- Remember the last few links in the input box.
- Windows: deleting a job while its preview is still streaming can leave files behind (Windows refuses
  to delete open files); retry the cleanup later.
- Key detection: report a second candidate (relative major/minor) when confidence is low.
- Bookmarklet in the desktop window: the port can change if 8765 is busy — offer a fixed-port option.
