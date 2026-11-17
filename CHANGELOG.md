# Changelog

## Unreleased

- In the app window, choose one or several files or a folder from New, choose the output folder
  in Settings, and pick files to add to the Library. New reads files chosen in the app where they
  are, so they need no upload and the browser upload size limit does not apply. A folder adds up
  to 50 supported files at its top level.
- If the app window fails to open, AudioLab stops its local server and reports the failure.

- Library lookups reject a damaged record whose ID differs from its filename.

- Command-line batches continue past missing inputs. A failed or cancelled command cleans up its own unfinished exports.

- Enhance keeps MP3, AAC (.m4a) and Opus files as loud as the master unless their peaks really go over:
  encoding may use the master's headroom up to just under full scale (−0.1 dBTP), and only a file over that
  is encoded once more, turned down by the excess plus 0.5 dB. On a test song, MP3 was 0.9 dB, AAC 1.2 dB
  and Opus 1.6 dB quieter than the WAV (measured loudness); now MP3 is as loud, AAC 0.7 dB and Opus 1.1 dB
  quieter. Check judges a lossy file's peaks by the same limit.
- Check reports measurements with consistent words for loudness and loudness range. It flags runs near
  full scale as possible clipping and identifies above-full-scale samples in the floating-point analysis copy.
  Its frequency-range estimate counts highs in both channels, including opposite-phase highs.
- WMA Lossless sources count as lossless: in Enhance, a dull top end can get the gentle high-end lift, as
  with FLAC or WAV.

## 2.5.1 (beta), 2026-11-16

- Enhance's middle choice is now called "Tone and volume" (formerly "Adaptive"); what it does is unchanged.
- Save / Convert to MP3, M4A or Opus on another drive needs room there only for that file, so a one-hour
  MP3 now starts on a stick with 1 GB free. WAV, FLAC, the original stream, video and cutting into tracks
  still need the full working space there.

## 2.5.0 (beta), 2026-11-16

- **Adjust**, new in the Library: pick a 5–30 second passage of a version, edit its EQ on a graph, in fields
  or as text, add section fades, and hear saved against edited settings on the same passage. Auditions are
  estimates; Apply to full recording renders and checks a new version and keeps the earlier ones.
- **Updates & add-ons** replaces "AI & system" in Settings. "Check now" says whether a newer AudioLab or
  downloader exists and links to its release page; "Check for updates automatically" (off by default) asks
  once a day. Nothing installs itself.
- The Mac app can **update its downloader**: "Update downloader" fetches yt-dlp's newest stable release from
  GitHub and checks its size, checksum and that it starts; the built-in copy (yt-dlp 2026.10.26) stays as
  the fallback ("Use built-in version"). A link that fails because the site changed offers the button.
- AI add-on: stays ready when only its download address changes, explains a missing download or connection
  in plain words, stops a slow download at once on Cancel, shows one size, and offers Repair only when broken.
- **Safer files:** a job that fails or is cancelled while still working removes the files and folder that
  attempt saved, so "Try again" reuses the names; nothing else is touched, and a finished result stays even
  if the Library step is interrupted. At start, AudioLab clears crash leftovers in its app data. For Enhance
  and Split the output drive needs room only for the results; Save / Convert still needs the full working
  space there. The output folder may not lie inside app data: one set there by an earlier version is refused
  before a job starts (choose another in Settings → Files). A job that can't be saved is refused at once.
- In Enhance, MP3, AAC and Opus files (and their tracks) that the encoder pushes over your peak ceiling are
  encoded once more, at most 2 dB quieter; lossless files are never re-encoded. (Save / Convert and Split
  never do this.)
- **Less memory for long recordings:** on an 8 GB Mac, Volume only and Adaptive Enhance now start up to the
  8-hour limit (before: refused from about one hour), working in files on disk; AI stems is unchanged.
- **Faster:** cutting long WAV or FLAC into tracks, Enhance's measurements, and Cancel in Split's last steps.
- **Simpler New:** a file starts on Enhance, a link on Save. Name and cover art moved into More options;
  Tracks shows up front only with chapters, a pasted tracklist or over 10 minutes. A link opened from another
  page (`/?url=…`) waits until you press Load.
- **Plain words:** in the app, results lead with Show in Finder (downloads and ZIP only when AudioLab runs on
  another computer); LUFS/dBTP figures appear with Show details. Settings groups kept originals under Storage
  and says "app data" everywhere. The "Send to AudioLab" bookmarklet is gone: paste or drag a link instead.
- **Library and window:** a ⋯ menu per recording ("Add a version from a file", "Delete the kept original");
  the sidebar starts collapsed under 1000 px; after failures in a batch, Start retries only those; without
  the AI add-on, Split links to setting it up. The interactive API page (`/api/docs`) is gone.
- **Fixes since 2.4.0b6:** unchanged-setting previews play the saved export; stereo checks and automatic mono
  bass cover the whole recording; low-mid control leaves events at the very start or end alone; version
  comparison turns volume matching off when a measurement is missing; "Check the result" uses only the
  finished file; a start after a crash no longer hands over to another program on the old port.

## 2.4.0 betas (b1–b6), 2026-10-20 to 2026-11-09

- **b1:** the first self-contained Mac app (macOS 14+, Apple silicon; ad-hoc signed, not notarized) with
  Save/Convert, Check, Enhance and the Library, and a public source beta with macOS, Windows and Linux CI.
  A new look ("Sleeve"), a sleeve preview on New, one compare player that keeps both versions on one clock,
  "Change and start again", and many fixes to cancellation, uploads, errors, covers and playback.
- **b2–b3:** the optional AI add-on (one download with its own runtime and both models, for offline Split and
  AI stems) with progress, resume, cancel, repair and removal; later builds reuse an installed AI 1.0.0.
  Library comparisons keep position and volume matching between versions; clearer cover-art controls.
- **b4:** whole-file loudness and peak checks for long recordings, adaptive low-mid control instead of a fixed
  cut, opt-in bass harmonics, Volume only / Adaptive / AI stems, a loudness and peak check per export, a
  de-esser, and processing details kept in the Library after jobs expire.
- **b5:** a changes timeline in comparisons, Library previews of changed settings on a passage with Apply to
  full recording, and disk-backed buffers for Enhance beyond 10 minutes.
- **b6:** a tone timeline, custom and section EQ, Library versions that say what they are, matched-volume
  "Compare versions", "Add a file…", moving removed files to the Trash (opt-in), reuse of kept originals for
  links, the delivered stream's details, "Check the result", destination-named format presets, a 24 dB/octave
  rumble filter at 20 Hz, and dynamics choices that no longer mistake a long DJ set's drift for a concert.

Versions 2.0 to 2.3 (October 2026) are described in earlier versions of this file.
