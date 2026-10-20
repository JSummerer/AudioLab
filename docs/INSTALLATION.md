# Install AudioLab

**Trying the Mac app? Start with the [Mac setup guide](MACOS.md).** It covers the direct download,
first opening, optional AI, updates and removal without Terminal commands.

Choose the self-contained **Mac app** for everyday testing on Apple silicon, or install from
source for development, Windows or Linux. They use the same processing code and normally
share app data for the same computer user; quit one before opening the other.

**On this page:** [Mac app](#mac-app) · [Source setup](#install-from-source) ·
[AI](#optional-ai) · [Source AI](#optional-ai-in-source-installations) ·
[Updates](#update-audiolab) · [Troubleshooting](#troubleshooting) · [Uninstall](#uninstall)

## Mac app

The initial binary target is **macOS 14 or later on Apple silicon (arm64)**. No Python installation,
Terminal setup or Apple developer account is required. Intel support is not yet qualified.

Download the [**v2.4.0b2 Mac app**](https://github.com/JSummerer/AudioLab/releases/download/v2.4.0b2/AudioLab-2.4.0b2-macOS-arm64.dmg).
Open it, drag **AudioLab.app** into **Applications**, eject the image and open the app from Applications.
This is the only file testers need. **Source code (zip)** is for the separate source setup below.

This beta uses ad-hoc signing and is not Apple Developer ID signed or notarized. For the expected
first-opening warning, use the [Mac guide's graphical Open Anyway steps](MACOS.md#first-opening).
That guide also covers updates, storage and uninstall. The
[release notes](https://github.com/JSummerer/AudioLab/releases/tag/v2.4.0b2) describe testing and beta limitations.

The **2.4.0b2** app includes **Save / Convert**, **Check**, and **Light/Standard Enhance**, plus the
Library. Settings offers the [optional managed AI download](#optional-ai) for Split and Best Enhance.
The main app and its downloader update by replacing the app.

## Install from source

Source setup uses Python 3.10–3.13, directly or through [uv](https://docs.astral.sh/uv/). It downloads
dependencies into `.venv` inside a writable project folder, including an FFmpeg fallback. If uv obtains
Python, that Python and uv's cache live outside the project folder. Keep the folder after setup.

1. On the repository page, select **Code → Download ZIP**.
2. Extract it: double-click the ZIP on macOS; on Windows, right-click and choose **Extract All**.
   Open the extracted folder rather than the ZIP preview.
3. Move the folder somewhere permanent and writable, such as your home folder. Avoid system folders
   such as Program Files. Keep the setup window open until AudioLab appears.

### macOS source launcher

1. Double-click **AudioLab.command** in the extracted project. This source setup opens Terminal.
2. If it offers to install uv, read the prompt and accept or decline. uv can obtain a compatible
   Python; an existing compatible installation may be used instead.
3. Wait for setup. It creates **AudioLab.app** in **Applications inside your home folder**, then opens
   a native window. Later starts reuse the source environment.

This Applications entry is a shortcut to the project, unlike the downloadable app. If you move
the project folder, run `AudioLab.command` there again to refresh it. Keep separate app copies clearly
named if you use both source and packaged builds. The window integration uses macOS APIs from 11.3;
current Python/dependency versions can impose a newer OS floor. The binary release targets macOS 14+.

For a browser window, open a terminal in the project folder and run:

```bash
bash start.sh --browser
```

### Windows source launcher — experimental

Use the browser version first. The complete Windows installation/runtime path still needs native
qualification; successful Mac checks do not establish Windows compatibility.

1. If uv or Python 3.10–3.13 is already installed, continue to step 2. Otherwise install uv in PowerShell:

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   This is the [official uv WinGet package](https://docs.astral.sh/uv/getting-started/installation/#winget).
   If WinGet is unavailable, follow the [uv Windows instructions](https://docs.astral.sh/uv/getting-started/installation/).
   Close and reopen Terminal afterwards.
2. Double-click **start.bat** in the extracted project. It creates the private environment and opens
   your browser. With uv, missing Python 3.12 is obtained automatically.
3. Keep the terminal open while using AudioLab. Press **Ctrl+C** there to stop it; confirm if asked.

If `start.bat` cannot find uv, run `.\start.bat` in a new PowerShell window in the project folder.
Python 3.14 or newer alone does not meet this release's source requirements; use uv to obtain a
supported version without removing your other Python.

### Linux source launcher — experimental

Open a terminal in the extracted folder and run:

```bash
bash start.sh
```

It uses uv or Python 3.10–3.13, offering to install uv if needed. If Python cannot create a virtual
environment, follow the message or use uv. Keep the terminal open; press **Ctrl+C** to stop AudioLab.
Distribution-specific setup remains unqualified.

## Optional AI

AI adds **Split** and **Best Enhance**. It is optional; the other tools work without it.
The managed packaged option is included in **2.4.0b2**, targeting macOS 14+ on Apple silicon (arm64).
The older **2.4.0b1** app has no AI installer; replace it with the newer app first.

### Packaged Mac app — 2.4.0b2

Open **Settings → AI & system → Enable AI features**. When a compatible add-on is available for
that app build, Settings shows its download and installed-file sizes. These describe the release
asset, not the RAM needed to process audio. Allow additional free space for downloading, unpacking
and audio intermediates; no fixed download-size or minimum-RAM promise applies to every release.
The 2.4.0b2 add-on is about **570 MB to download** and **1.02 GB installed**.

The optional download is self-contained: it includes its own AI runtime and both **Standard** and
**Fine-tuned** model choices. AudioLab verifies the download and tests the runtime before enabling
it. After installation, both choices work offline with local files; processing stays on your Mac.
It needs no Terminal, separately installed Python, Homebrew, uv or pip. It does not change those
shared tools or adopt their model caches.

Settings shows progress and recent error details. **Cancel install** stops work and keeps a partial
download for **Resume AI download**. Retry a failed installation after checking your connection and
free space. **Repair** reinstalls and verifies the compatible add-on. **Update AI add-on** appears
when the app's bundled release catalog specifies another version; the app never silently chooses
an arbitrary latest add-on. A build without a compatible catalog explains why installation is
unavailable.

**Remove AI add-on** asks for confirmation, then removes AudioLab-owned AI software, models, caches
and partial downloads from the app-data folder's `ai/` directory. Your recordings, Library,
settings and source-installed/shared AI packages stay. Finish or cancel an AI job before changing
its installation. You can enable AI again later.

### Resource notes

AI can consume several gigabytes of working memory and considerably more temporary audio space
than the model files alone. Hardware requirements and performance are not yet qualified. Settings
reports detected acceleration; CPU-only processing is slower and limited to recordings of 20 minutes
by default. Try a short local recording first. Shorter recordings and Light/Standard Enhance reduce
resource demand. Fine-tuned uses several models and is slower than Standard.

## Optional AI in source installations

Source installations keep the existing package installer. Enable AI in Settings for Split and Best
Enhance, or request it during setup:

```bash
bash start.sh --with-ai
```

On Windows: `.\start.bat --with-ai`. AI software installs first; model weights download on first use.
Processing then stays on your computer. The packaged Mac app uses the separate managed download
described above; it never runs this source installer.

For scale, one audited Apple silicon environment with Torch 2.14.1 and Demucs 4.1.0 added about
**585 MB of installed AI package files**, before weights and installation overhead. The author's
[Standard model](https://huggingface.co/adefossez/HTDemucs/tree/main) adds about **84 MB**;
[Fine-tuned](https://huggingface.co/adefossez/HTDemucs-ft/tree/main) uses four models totaling about
**336 MB**. Those are on-disk figures, not a complete compressed download size or RAM requirement.
Versions and cache copies change the total. The [Demucs guide](https://github.com/adefossez/demucs)
describes Fine-tuned as roughly four times slower than Standard.

## Update AudioLab

For the **Mac app**, follow [Replace the app](MACOS.md#updates): quit, install the newer DMG's app
and reopen. Its downloader updates with the app. Check **Settings → AI & system**
for any compatible AI add-on update after replacing the app. Source packages and shared caches are
not changed by the packaged AI updater.

For a **source installation**:

1. Quit AudioLab and stop its terminal if using the browser version.
2. Extract the newer source into a new folder. Keep the old folder until the new copy opens and your
   Library/results are available.
3. Run the platform launcher. On macOS it refreshes the source shortcut. Dependencies may download
   again; request `--with-ai` in the new copy if needed.

Settings and Library normally live outside the source folder. If output or data was configured inside
the old project, preserve those files before deleting it. Docker's example also puts output inside
the project. A Git checkout can instead be updated in place and its launcher rerun. The launcher
refreshes base dependencies when `pyproject.toml` changes; use `--with-ai` for changed AI requirements.
Source mode's **Update downloader** updates the link-handling tool, not AudioLab itself.

## Troubleshooting

| Problem | What to do |
|---|---|
| The downloaded Mac app is blocked on first open | Read [First opening](MACOS.md#first-opening). A damaged/malware alert needs investigation rather than the normal opening exception. |
| A packaged app will not start | Record the message, Mac chip, macOS version and exact release/asset name. Try a fresh copy from the same release; see [Mac troubleshooting](MACOS.md#troubleshooting). |
| Source setup fails during a download/install | Keep the last error lines, check your connection/free space, then rerun the launcher. |
| The source Mac shortcut cannot open | The project may have moved. Run `AudioLab.command` in its current location. |
| The source Python environment is broken | With AudioLab stopped, run `bash start.sh --reinstall`; Windows: `.\start.bat --reinstall`. This recreates `.venv`; enable AI afterwards if needed. |
| The browser did not open | Use the address printed in the source terminal. Keep it running; the port can vary. |
| Split or Best is unavailable | Update an older 2.4.0b1 app first. In 2.4.0b2, open Settings → AI & system to install, resume or repair the add-on and inspect errors. Source users can enable the existing AI package installer. |
| The output folder is unavailable | Reconnect its drive or choose another folder in Settings → Files. |
| A result plays elsewhere but not here | Codec support varies. Open the result in your usual player or export MP3/WAV. |

Once AudioLab opens, **Settings → About → Copy system report** helps with support. Review it before
posting: names, links and tool output may be personal. Never share passwords, cookies or access tokens.

## Uninstall

For the downloaded app, see [Mac uninstall and optional cleanup](MACOS.md#uninstall).

For source installations, quit first and check **Settings → Files** for your output folder. Preserve
audio/backups inside the project, including Docker's `output/`, before removing the source folder and
any source shortcut. Keep originals and Library data if you want them after reinstalling.

| Data | Default location |
|---|---|
| Finished audio — all installations | `~/Music/AudioLab`, or the folder you chose |
| Settings, jobs, Library and kept originals — macOS | `~/Library/Application Support/AudioLab` |
| Same — Windows | `%LOCALAPPDATA%\AudioLab` |
| Same — Linux | `~/.local/share/audiolab`, or under `XDG_DATA_HOME` |
| App logs — macOS | `~/Library/Logs/AudioLab` |
| Managed packaged AI software, models and partial downloads | The app-data folder’s `ai/` subfolder; use Settings → AI & system → Remove AI add-on |
| Source-mode AI weights | Hugging Face and/or Torch caches, usually under `~/.cache`; may be shared |

Removing app data loses history and kept original copies. Output files are never removed automatically.
Do not remove shared Python, uv or model caches just to uninstall AudioLab. Custom environment-variable
folders and Docker volumes have their own locations; see the [server notes](USAGE.md#docker--home-server).

## Manual Python installation

For development or an existing Python setup, use Python 3.10–3.13 in the project folder:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m audiolab
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m audiolab
```

Use `-e ".[ai]"` to include the optional AI pack. macOS installs the native-window dependency by
default. Optional native windows elsewhere (`.[desktop]`) can need system components and remain
outside the novice setup path. See [CONTRIBUTING.md](../CONTRIBUTING.md) for development checks.
