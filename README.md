# AudioLab

**Convert, enhance, split and inspect audio on your own computer.**

Work with your recordings, audio/video files and supported web links. Adjust loudness, export useful
formats, separate vocals and instruments, and keep your results together in a local Library.
No AudioLab account or cloud processing service is required.

**2.4 beta:** macOS is the primary platform. Expect rough edges;
please report reproducible problems through [GitHub Issues](https://github.com/JSummerer/AudioLab/issues).
AudioLab is a working project name, not a claim of affiliation with other apps using that name.

## Download for Mac

**[Download AudioLab 2.4.0b2 for Mac](https://github.com/JSummerer/AudioLab/releases/download/v2.4.0b2/AudioLab-2.4.0b2-macOS-arm64.dmg)** · about 106 MB

For **Apple silicon Macs with macOS 14 or later**. Check **Apple menu → About This Mac**:
look for a **Chip** entry, such as Apple M1 or later, and your macOS version.
There is no packaged Intel, Windows or Linux app yet.

1. **Download** using the link above. This is the only file you need from GitHub.
2. **Install:** open the downloaded `.dmg` file and drag **AudioLab.app** into **Applications**.
   Eject the AudioLab disk image in Finder.
3. **Open AudioLab** from Applications. No Terminal, Python installation or AudioLab account is needed.

**Blocked on first opening?** This personal beta has not been verified by Apple. After trying to open
it, follow [System Settings → Privacy & Security → Open Anyway](docs/MACOS.md#first-opening).
That guide explains the expected warning and when to stop.

**AI is optional:** Save / Convert, Check and Light/Standard Enhance work immediately.
For Split and Best Enhance, choose **Settings → AI & system → Enable AI features** and wait for
**Ready**. AudioLab downloads and manages the extra files for you; no separate installer is needed.
Allow roughly **0.6 GB to download and 1 GB installed**, plus extra space for installation and audio work.
You can resume an interrupted download or remove the add-on in Settings.

[Mac setup and help](docs/MACOS.md) · [First recording](#your-first-recording) ·
[Updates](docs/MACOS.md#updates) · [Uninstall](docs/MACOS.md#uninstall) ·
[Release notes](https://github.com/JSummerer/AudioLab/releases/tag/v2.4.0b2)

![AudioLab with a local audio sample ready to enhance](docs/img/app.png)

## What can I do?

| Task | What it does |
|---|---|
| **Save / Convert** | Convert a file or save audio from a supported link as MP3, M4A, FLAC, WAV, Opus or the original format. |
| **Enhance** | Adjust loudness, tone and peaks using a recipe for music, live recordings or speech. Compare the result with the original. |
| **Split** | Separate vocals and instruments, or make four stems: vocals, drums, bass and other. Requires the optional AI pack. |
| **Check** | Inspect loudness, peaks, dynamics and frequency range; view a spectrogram and estimated tempo/key. |

You can add several files, edit names and cover art, or cut a long recording using chapters or a
tracklist. **Library** keeps finished Save, Enhance and Split results with their versions.
Enhancement cannot restore information missing from a recording; use the comparison player to decide
which version you prefer.

## Your first recording

1. Choose **New → Choose files** and select an audio file, or paste a supported web link and press **Load**.
2. Pick **Convert** for a file (**Save** for a link), **Enhance** to process it, or **Check** to inspect it. Defaults are a good
   starting point; **Light** enhancement mainly adjusts volume.
3. Press the main action button. Follow progress in **Activity**, then play or download the result.

Finished audio goes to **`Music/AudioLab`** in your home folder. Change that in **Settings → Files**.
Existing output files are kept; a new result gets a numbered name if needed. Removing a Library entry
preserves your output files, but may remove its kept original copy when no other entry uses it.
When enabled, kept original copies live separately in AudioLab's app-data folder and can also be
cleared in **Settings → Storage**.

See [optional AI](docs/MACOS.md#optional-ai) for download progress, removal and hardware notes.
The add-on includes both model choices; local files can be processed offline once it is installed.

## Help and feedback

- **Setup stops:** use [Mac troubleshooting](docs/MACOS.md#troubleshooting).
  Source installations have [separate help](docs/INSTALLATION.md#troubleshooting).
- **A job fails:** use **Copy details for a bug report** on the job. For general problems, use
  **Settings → About → Copy system report**. Include your operating system and the steps that led to the problem.
  Review the report before sharing: filenames, source links or tool output may be personal.
- **Security concern:** follow [SECURITY.md](SECURITY.md) to report it privately.
- **A link fails:** site support varies and can change. For the Mac app, check for a newer app release
  or use a local file. Source installations also offer **Update downloader** in Settings. AudioLab does
  not promise support for every site or restricted link.
- **Want more control?** See [usage, command-line and server options](docs/USAGE.md).

Use recordings and links you have permission to process, and follow their licenses and the source
service's terms. AudioLab is not affiliated with any content-hosting service.

## Install from source

For development or experimental Windows/Linux use, follow the [source installation guide](docs/INSTALLATION.md#install-from-source).
This route uses Terminal and installs dependencies. Mac testers can use the downloadable app above.

## For contributors

Start with [CONTRIBUTING.md](CONTRIBUTING.md). See the [architecture](docs/ARCHITECTURE.md),
[HTTP API](docs/API.md), [changelog](CHANGELOG.md) and [roadmap](docs/ROADMAP.md) for more detail.

<details>
<summary>Advanced: server settings</summary>

These environment variables are for command-line/server setups. Most desktop users can use Settings.

| Variable | Default | Meaning |
|---|---|---|
| `AUDIOLAB_OUTPUT_DIR` | `~/Music/AudioLab` | Where finished files go (locks the setting in the UI) |
| `AUDIOLAB_DATA_DIR` | platform app-data folder | Jobs, settings, tool shims |
| `AUDIOLAB_TOKEN` | auto when not on localhost | Access token; `none` disables it |
| `AUDIOLAB_MAX_CONCURRENT_JOBS` | 1 | Jobs processed at the same time |
| `AUDIOLAB_MAX_DURATION_S` | 28800 | Longest accepted recording |
| `AUDIOLAB_AI_CPU_MAX_DURATION_S` | 1200 | Longest AI job without a GPU (`AUDIOLAB_ALLOW_UNSAFE_AI=1` overrides) |
| `AUDIOLAB_MAX_UPLOAD_MB` / `AUDIOLAB_MAX_DOWNLOAD_MB` | 4096 | Size caps |
| `AUDIOLAB_MAX_PLAYLIST_ITEMS` | 200 | Playlist cap |
| `AUDIOLAB_MIN_FREE_DISK_GB` | 2 | Refuse a job that would leave less free disk |
| `AUDIOLAB_MEMORY_SAFETY_FRACTION` | 0.85 | Share of memory a job may plan to use |
| `AUDIOLAB_RETENTION_DAYS` | 30 | Delete old job records (never your output files) |

Timeouts can also be set: `AUDIOLAB_FFMPEG_TIMEOUT_S`, `AUDIOLAB_YTDLP_PROBE_TIMEOUT_S` and
`AUDIOLAB_YTDLP_DOWNLOAD_TIMEOUT_S`.

</details>

## License and credits

AudioLab's code is available under the [MIT license](LICENSE). Dependencies retain their own licenses.
Built with [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org),
[Demucs](https://github.com/adefossez/demucs), [Deno](https://deno.com), NumPy/SciPy, soundfile,
FastAPI and pywebview. The bundled Source Serif font includes its [license](audiolab/web/fonts/OFL.txt).
The Mac app includes dependency notices and a matching source companion in its release.
Source installations use [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) as a fallback audio engine.
