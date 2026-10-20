# Install AudioLab

AudioLab is distributed as source code with launchers that set it up for you. There is no signed,
standalone installer yet. **macOS is the primary beta platform**; Windows and Linux browser launchers
are experimental. Their complete installation/runtime paths have not been verified on those systems.

You need an internet connection for setup and a folder you can write to. Save, Check and Light/Standard
Enhance work without the optional AI pack. Setup installs the core dependencies, including an FFmpeg
fallback, in `.venv` inside the project folder. If uv installs Python, that Python and uv's cache live
outside the project folder.

## Download the project

1. On the repository page, select **Code → Download ZIP**.
2. Extract it: on macOS, double-click the ZIP; on Windows, right-click it and choose **Extract All**.
   Open the extracted folder, not the ZIP preview.
3. Move the folder somewhere permanent and writable, such as your home folder. Avoid system folders
   such as Program Files. Keep this folder after setup: the app runs from it.

The first start downloads dependencies and may take several minutes. Keep the setup window open until
AudioLab appears. Later starts normally reuse the installed environment.

## macOS

The native window requires macOS 11.3 or newer. Fresh-install checks currently cover macOS 26.6.2
on Apple Silicon.

1. In the extracted folder, double-click **AudioLab.command**. A Terminal window opens.
2. If it asks to install **uv**, read the prompt and press Return to accept, or enter `n` to decline.
   [uv](https://docs.astral.sh/uv/) can download a compatible Python; an existing compatible Python may
   be used instead.
3. Wait for setup to finish. AudioLab adds **AudioLab.app** to **Applications inside your home folder**
   and opens its own window.
4. Next time, open that app, or search for **AudioLab** in Spotlight. You can keep it in the Dock.

The Applications entry is a launcher, not a self-contained copy. If you move the project folder,
double-click `AudioLab.command` in its new location to refresh the launcher.

### If macOS blocks the first opening

For an unidentified-developer or unnotarized-app warning, first confirm you obtained the project from
the repository you intended to use. After trying to open it, go to **System Settings → Privacy & Security**
and use **Open Anyway**, if offered. See [Apple's opening instructions](https://support.apple.com/en-us/102445).
Do not disable Gatekeeper globally. A malware or damaged-file alert is different: stop and check the
download rather than overriding it.

If Terminal reports **Permission denied**, open Terminal, type `cd ` (with a trailing space), drag the
extracted AudioLab folder onto the Terminal window and press Return. Then run:

```bash
chmod +x AudioLab.command start.sh
bash start.sh
```

To run in a browser instead of the app window, from that same folder:

```bash
bash start.sh --browser
```

## Windows — experimental

Use the browser version first. No native app window or desktop shortcut is required.

1. Extract the project as described above.
2. If **uv** or **Python 3.10–3.13** is already installed, skip to step 3. Otherwise open
   **Terminal → PowerShell** and install uv:

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   This is the [official uv WinGet package](https://docs.astral.sh/uv/getting-started/installation/#winget).
   If `winget` is unavailable, follow the Windows steps on the
   [uv installation page](https://docs.astral.sh/uv/getting-started/installation/).
   Close and reopen Terminal after installing it.
3. Double-click **start.bat** in the extracted project folder. It creates a private Python environment,
   installs AudioLab and opens your default browser. With uv, missing Python 3.12 is downloaded automatically.
4. Keep the terminal window open while using AudioLab. Press **Ctrl+C** in it to stop the app; confirm
   termination if Windows asks.

If `start.bat` cannot find uv after installation, open a new PowerShell window in the AudioLab folder
and run `.\start.bat`. An existing Python 3.14 or newer is not sufficient for this release; use uv to
obtain the supported version rather than removing your other Python.

Windows-specific file handling, cancellation and dependency installation still need native qualification.
If something fails, share the error and Windows version; a successful macOS test does not establish
Windows compatibility.

## Linux — experimental

Open a terminal in the extracted folder:

```bash
bash start.sh
```

It uses uv or a compatible Python 3.10–3.13. If Python is missing, it offers to install uv. If it reports
that Python cannot create a virtual environment, follow the message or use uv. Keep the terminal open;
press **Ctrl+C** there to stop AudioLab. Distribution-specific dependencies remain unqualified.

## Optional AI features

Use **Settings → Enable AI features** for Split and Best enhancement. Allow several gigabytes of free
disk space: the software is installed first, and model files may download when you first run an AI job.
The job then processes audio locally.

You can also request the pack during setup:

```bash
bash start.sh --with-ai
```

On Windows, from PowerShell: `.\start.bat --with-ai`.

A compatible Apple Silicon or NVIDIA GPU can speed up processing. Availability depends on the installed
PyTorch build, hardware and drivers; Settings reports what AudioLab detects. CPU processing is available
but can be slow, and long AI jobs on CPU are limited. Try the core app before installing the AI pack.

## Update AudioLab

1. Quit AudioLab and stop its terminal if you use the browser version.
2. Download and extract the newer project **into a new folder**. Keep the old folder until the new copy
   opens and your Library/results are available.
3. Start the new copy with its platform launcher. On macOS this refreshes the Applications entry.
   Setup may download dependencies again; enable AI in the new copy if you need it.

Settings and Library records normally live outside the project folder and are reused by the same
computer user. Files remain at their existing locations. If you chose an output or data folder inside
the old project, keep it: do not delete the old folder until those files are safely preserved.
The Docker example also stores output inside the project by default.

For a Git checkout, updating the existing checkout and rerunning the launcher is an alternative.
The normal launcher refreshes base dependencies when `pyproject.toml` changes. If an update changes AI
requirements, rerun the launcher with `--with-ai` to refresh those optional dependencies as well.
**Update downloader** in Settings updates the link-handling tool; it does not update AudioLab itself.

## Troubleshooting

| Problem | What to do |
|---|---|
| Setup fails while downloading/installing | Keep the last error lines, check your connection and free disk space, then rerun the same launcher. |
| The Mac app says it cannot open | The project folder may have moved. Open `AudioLab.command` in its current location. App logs are in `~/Library/Logs/AudioLab`. |
| The browser did not open | Open the local address printed in the terminal. Keep the terminal running; do not assume the port is always the same. |
| A moved or broken Python environment | With AudioLab stopped, run `bash start.sh --reinstall` from its folder; on Windows use `.\start.bat --reinstall`. This recreates `.venv`; re-enable AI afterwards if needed. |
| Split or Best is unavailable | Enable the optional AI pack in Settings. Read any install error before retrying. |
| The output folder is unavailable | Reconnect its drive or choose an available folder in Settings → Files. |
| A file plays in another app but not AudioLab | Browser codec support varies. Download/open the result in your usual player, or export a compatible format such as MP3 or WAV. |

For setup failures, copy the visible terminal error. Once AudioLab opens, **Settings → Copy system
report** helps with support. Review reports before posting: filenames, links and tool output may be
personal. Never include passwords, cookies or access tokens.

## Uninstall

Quit AudioLab first. Check **Settings → Files** for your output folder before removing anything.
Keep any recordings, exports or backups stored inside the project folder, including Docker's `output/`.
You can then remove the project folder and, on macOS, the **AudioLab.app** launcher in your home
Applications folder.

This leaves app data and AI caches in place so a reinstall can find them:

| Data | Default location |
|---|---|
| Finished audio | `~/Music/AudioLab` (or the folder you chose) |
| Settings, job history, Library records and kept originals — macOS | `~/Library/Application Support/AudioLab` |
| Same — Windows | `%LOCALAPPDATA%\AudioLab` |
| Same — Linux | `~/.local/share/audiolab` (or under `XDG_DATA_HOME`) |
| App logs — macOS | `~/Library/Logs/AudioLab` |
| AI model cache | Usually `~/.cache/torch`; it may be shared with other software |

Delete app data only if you also want to lose that history and the kept original copies. Avoid removing
shared Python, uv or model caches just to uninstall AudioLab. Docker volumes and any folders selected
through environment variables have their own locations; see the [server notes](USAGE.md#docker--home-server).

## Manual Python installation

For developers or an existing Python setup, use Python 3.10–3.13 in the project folder:

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

Use `-e ".[ai]"` instead of `-e .` to include the optional AI pack. macOS installs the native-window
dependency by default. Windows/Linux use a browser; optional native windows (`.[desktop]`) may require
additional system components and are outside the novice setup path.
