# AudioLab

**Convert, enhance, split and inspect audio on your own computer.**

Work with your recordings, audio/video files and supported web links. Adjust loudness, export useful
formats, separate vocals and instruments, and keep your results together in a local Library.
No AudioLab account or cloud processing service is required.

**Public source beta:** development toward 2.4, with macOS as the primary platform. Expect rough edges;
please report reproducible problems through [GitHub Issues](https://github.com/JSummerer/AudioLab/issues).
AudioLab is a working project name, not a claim of affiliation with other apps using that name.

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

## Install and open

**macOS is the primary beta platform.** Windows and Linux have browser launchers, but their installation
and runtime behavior are still experimental. You do not need the AI pack to use Save, Check or
Light/Standard Enhance.

1. On this repository's page, choose **Code → Download ZIP**, then extract the ZIP.
2. Move the extracted folder somewhere you can keep it, such as your home folder. AudioLab runs from
   this folder; keep it after setup.
3. Follow the row for your computer:

| Computer | Start AudioLab |
|---|---|
| **macOS** | Double-click **`AudioLab.command`**. Setup opens in Terminal, installs the required tools and creates **AudioLab.app** in your home folder's **Applications** folder. After setup, use that app or Spotlight. |
| **Windows — experimental** | Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you do not already have Python 3.10–3.13 or uv, then double-click **`start.bat`** in the extracted folder. AudioLab opens in your browser. |
| **Linux — experimental** | Open a terminal in the extracted folder and run **`bash start.sh`**. AudioLab opens in your browser. |

Allow several minutes and an internet connection for the first setup. The launcher installs AudioLab
into a private environment in its folder and includes an FFmpeg fallback. On macOS/Linux, if a suitable
Python is missing, it asks before installing uv to obtain one. Setup time and download size vary.
This ZIP contains source code and setup scripts, not a signed, self-contained application installer.
For the browser version, keep the terminal open while using the app; press **Ctrl+C** there to quit.

**Need help getting started?** The [installation guide](docs/INSTALLATION.md) has step-by-step Windows
setup, macOS opening/permission help, updates, repair and uninstall instructions.

## Your first recording

1. Choose **New → Choose files** and select an audio file, or paste a supported web link and press **Load**.
2. Pick **Convert** for a file (**Save** for a link), **Enhance** to process it, or **Check** to inspect it. Defaults are a good
   starting point; **Light** enhancement mainly adjusts volume.
3. Press the main action button. Follow progress in **Activity**, then play or download the result.

Finished audio goes to **`Music/AudioLab`** in your home folder. Change that in **Settings → Files**.
Existing output files are kept; a new result gets a numbered name if needed. Removing a Library entry
only removes its record, not your output files. When enabled, kept original copies live separately in
AudioLab's app-data folder and can be cleared in **Settings → Storage**.

**AI is optional.** Enable it in Settings for Split and Best enhancement. It downloads additional
software and, on first use, model files; allow several gigabytes of free space. Processing stays on
your computer. Compatible Apple Silicon/NVIDIA setups can use a GPU; CPU processing can be much slower.

## Help and feedback

- **Setup stops:** keep the error message and use the [installation troubleshooting steps](docs/INSTALLATION.md#troubleshooting).
- **A job fails:** use **Copy details for a bug report** on the job. For general problems, use
  **Settings → Copy system report**. Include your operating system and the steps that led to the problem.
  Review the report before sharing: filenames, source links or tool output may be personal.
- **Security concern:** follow [SECURITY.md](SECURITY.md) to report it privately.
- **A link fails:** site support varies and can change. Try **Settings → Downloads & sign-in → Update downloader**,
  or use a local file you already have. AudioLab does not promise support for every site or restricted link.
- **Want more control?** See [usage, command-line and server options](docs/USAGE.md).

Use recordings and links you have permission to process, and follow their licenses and the source
service's terms. AudioLab is not affiliated with any content-hosting service.

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
Built with [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org)
(via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)),
[Demucs](https://github.com/adefossez/demucs), [Deno](https://deno.com), NumPy/SciPy, soundfile,
FastAPI and pywebview. The bundled Source Serif font includes its [license](audiolab/web/fonts/OFL.txt).
