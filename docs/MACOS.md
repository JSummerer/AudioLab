# AudioLab for Mac testers

The packaged beta is **AudioLab 2.4.0b2**, targeting **Apple silicon (arm64), macOS 14 or
later**. It brings its own Python runtime and audio tools. Using it needs no Terminal commands,
Python installation, Apple developer membership or AudioLab account. Intel builds are not yet qualified.

The main app includes Save / Convert, Check, Light/Standard Enhance, comparison and the Library.
**Settings offers an optional managed AI download for Split and Best Enhance**. It includes its own
runtime and both model choices; it needs no Terminal, Python setup or changes to shared Homebrew/uv tools. Source users
keep their [existing AI installer](INSTALLATION.md#optional-ai-in-source-installations).

## Download and install

1. Visit [JSummerer/AudioLab Releases](https://github.com/JSummerer/AudioLab/releases).
2. Under the **v2.4.0b2** release's **Assets**, choose **AudioLab-2.4.0b2-macOS-arm64.dmg**.
   Check the release notes for known issues and the validation actually performed.
   Testers only need the DMG; GitHub's source archives are for developers.
3. Open the downloaded DMG, drag **AudioLab.app** into **Applications**, then eject the disk image.
4. Open **AudioLab** from Applications. Keep it in the Dock if you like.

The installed app is self-contained; it does not depend on a downloaded source folder. The release
uses an ad-hoc signature with bundle identifier `io.github.jsummerer.audiolab`. This is **not an Apple
Developer ID signature**, and the beta is **not notarized or verified by Apple**. A successful local
build/signature check does not establish first-opening behavior on someone else's Mac.

## First opening

macOS may warn that the developer cannot be verified or Apple cannot check the app. If you want to
run this beta, first make sure the download came from the repository and release you intended.
After attempting to open it:

1. Open **System Settings → Privacy & Security**.
2. Scroll to Security and look for the blocked AudioLab message. Choose **Open Anyway**, if offered.
3. Review the repeated warning and choose **Open** if you want to proceed. Enter any administrator
   approval directly in macOS if asked.

This creates an exception for this app. The availability and wording can vary, especially on a managed
Mac. See [Apple's opening guidance](https://support.apple.com/en-us/102445). If the message says the app
**will damage your computer**, was moved to Trash as malware, or is **damaged**, stop and check the
download/report the exact message. These differ from the expected unidentified-developer warning.

## Files and privacy

Your files are processed locally. Supported web links require network access to their source. Enabling AI explicitly downloads
the optional software and both model choices; after installation these work offline with local
files. The app runs a server on a loopback address for its own window, so the chosen port can vary.

| Data | Default location |
|---|---|
| Finished audio and saved cover pictures | `~/Music/AudioLab`, or your choice in Settings → Files |
| Settings, job history, Library records and kept original copies | `~/Library/Application Support/AudioLab` |
| Downloader/Deno caches and runtime temporary files | The app-data folder's `cache/` subfolder |
| Managed AI software, models, cache and partial downloads | The app-data folder’s `ai/` subfolder |
| App logs | `~/Library/Logs/AudioLab` |

In **Settings → About**, use **Open app data** and **Open logs** to find the actual
locations. A custom data folder keeps its logs in `logs/` beneath it. The work folder contains uploaded
copies and processing intermediates; kept originals in `library/originals/` support full-length
comparison. They can be valuable recordings and are not merely disposable caches. Check reports live
with their jobs until you download a separate copy.

**Settings → Storage** can clear finished jobs or explicitly delete kept originals. Removing a Library
entry preserves its output files; its kept original may go when no other entry uses it, as the dialog
explains. Any extra copies saved through a download dialog stay wherever you saved them.

macOS/WebKit also manages native window website storage, preferences and caches under the app's
identifier. Those are separate from AudioLab's app-data folder; this release does not claim all
operating-system traces are contained in one place.

## Optional AI — 2.4.0b2

Replace an older **2.4.0b1** app first; that release has no AI installer. In **2.4.0b2**, choose **Settings → AI & system → Enable AI features** to add Split and Best Enhance. Settings shows
the release's known download and installed sizes, progress and errors. Download size, installed
size, available RAM and temporary audio space are different requirements. No minimum hardware or
performance level has been qualified; begin with short audio.

Use **Cancel install** to stop and **Resume AI download** to continue a partial download. **Repair**
reinstalls the version selected by this app. **Update AI add-on** appears when the app's bundled
catalog specifies a different compatible version; it is not an independent automatic updater.
If no compatible add-on is available for the build, Settings explains why.

Use **Remove AI add-on** to clear its owned software, models, cache and partial downloads while
keeping your recordings, Library and settings. The confirmation describes what is removed. It
never removes source-installed packages or shared model caches. Finish or cancel an AI job first.
See [optional AI](INSTALLATION.md#optional-ai) for source setup and resource notes.

## Updates

1. Quit AudioLab. Keep the old app until you have obtained the new release.
2. Download the newer DMG from the same repository's Releases page and open it.
3. Drag its **AudioLab.app** into Applications and choose **Replace** when Finder asks. Eject the image
   and open the new app.
4. Check the version in **Settings → About** and that your Library and results are available.

Settings, Library and output normally remain outside the app and are reused. Read release notes for
any compatibility instructions. The app and its downloader update together; there is no automatic
pip installer or package self-update in the main app. For optional AI, check Settings after
updating the app: its bundled catalog controls the compatible AI version. A new app download can
require the first-opening exception
again. Avoid running source and packaged copies against the same data folder at the same time.
The source installer preserves a packaged or unrecognized app already at its shortcut destination.

## Uninstall

1. To remove only optional managed AI, use **Settings → AI & system → Remove AI add-on** first.
   Before quitting, check **Settings → Files** for your output folder. Use the About folder buttons
   if you want to inspect or preserve app data.
2. Quit AudioLab, then move **AudioLab.app** from Applications to Trash.

This removes the app. It leaves your output and app data available for a reinstall. To remove app
data as well, use Finder's **Go → Go to Folder…** and inspect these exact locations after quitting:

- `~/Library/Application Support/AudioLab` — removing it loses settings, job history, Library records
  **and kept original audio copies**. Preserve anything you want first. If you selected output inside
  this folder, preserve that output too.
- `~/Library/Logs/AudioLab` — app logs. A custom data folder instead contains its own `logs/`.

Move only the AudioLab folders you intend to remove to Trash. For a custom data path, use the path
shown by the app rather than guessing. **Never remove your output folder as part of app cleanup.**
Files saved to Music, a custom output location or a download dialog are yours to keep or remove
separately; AudioLab never automatically deletes them when uninstalling.

For optional OS-managed cleanup, Finder may also show these identifier-specific locations:

| Item, if present | Expected location |
|---|---|
| WebKit website data | `~/Library/WebKit/io.github.jsummerer.audiolab` |
| Native caches | `~/Library/Caches/io.github.jsummerer.audiolab` |
| Native preferences | `~/Library/Preferences/io.github.jsummerer.audiolab.plist` |
| Saved window state | `~/Library/Saved Application State/io.github.jsummerer.audiolab.savedState` |

These locations are controlled by macOS and can vary or be absent. Remove only exact app-identifier
items after quitting. Do not delete general WebKit, browser, Python or shared cache folders. macOS
can retain its own security, recent-item and system records; this is not a promise to erase every
trace. Users of older source installations should also read the
[source uninstall notes](INSTALLATION.md#uninstall); shared uv/Python/model caches are not owned
exclusively by this app.

## Troubleshooting

- **The app does not open:** record the exact warning, asset name, Mac chip and macOS version. Check
  that the asset is arm64 for Apple silicon and the OS meets the release requirement. Keep the
  downloaded original available for comparison and report the issue through
  [GitHub Issues](https://github.com/JSummerer/AudioLab/issues).
- **A job fails:** use the job's bug-report details. For general problems, use
  **Settings → About → Copy system report**. Review paths, names and links before sharing.
- **A link stops working:** check for a newer app release or use an audio file you already have.
  The app's bundled downloader is updated by replacing the app.
- **Split or Best is unavailable:** 2.4.0b1 has no AI installer; update the app first. In 2.4.0b2, use
  **Settings → AI & system** to install, resume or repair the managed add-on. Read the reason and
  error log if installation is unavailable or fails. It never starts a hidden first-run download.
- **Long audio needs too much memory or disk:** shorten it or use Light enhancement. There is no
  qualified minimum-RAM promise yet; resource use depends on task, duration and other running apps.

Build/release contributors: see the [macOS packaging guide](../packaging/macos/README.md).
