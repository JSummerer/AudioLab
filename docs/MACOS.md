# AudioLab for Mac

**[View the release on GitHub](https://github.com/JSummerer/AudioLab/releases/tag/v2.4.0b2)** · [Direct download (.dmg, 106 MB)](https://github.com/JSummerer/AudioLab/releases/download/v2.4.0b2/AudioLab-2.4.0b2-macOS-arm64.dmg) · public beta

No Terminal, Python installation or AudioLab account is needed. The app contains its own audio tools.
Save / Convert, Check, Light/Standard Enhance and the Library work without the optional AI download.

**On this page:** [Check your Mac](#check-your-mac) · [Install](#download-and-install) ·
[First opening](#first-opening) · [Optional AI](#optional-ai) · [Updates](#updates) ·
[Uninstall](#uninstall) · [Help](#troubleshooting) · [Files and privacy](#files-and-privacy)

## Check your Mac

This beta is for **Apple silicon with macOS 14 or later**.
Open **Apple menu → About This Mac** to see your chip and macOS version.
A **Chip** entry, such as Apple M1 or later, means Apple silicon; **Processor: Intel** means this
download is not for your Mac. See [Apple's identification guide](https://support.apple.com/en-us/116943).
There is no packaged Intel app yet. Windows/Linux users can try the experimental
[source installation](INSTALLATION.md#install-from-source).

Already have **8 GB RAM**? Start with short core tasks. For early AI testing we prefer existing Macs
with **16 GB or more**; capacity on 8/16 GB has not yet been tested.
Aim for **5 GB free disk space**, or **10 GB with AI**, after installation for short jobs.
These are planning recommendations, not tested minimums. Long recordings can need substantially more.
See [hardware and storage](HARDWARE.md) for memory, disk, model choices and the current checks' limits.

## Download and install

1. **Choose your download:** open the GitHub release page above and select the `.dmg` under **Assets**,
   or use **Direct download** to start immediately. The file is named
   **AudioLab-2.4.0b2-macOS-arm64.dmg** and normally appears in Downloads.
2. **Open the downloaded file.** In its window, drag **AudioLab.app** into **Applications**.
   Wait for Finder to finish copying, then eject the AudioLab disk image in Finder's sidebar.
3. **Open AudioLab from Applications.** If macOS blocks it, follow the next section.

That's the whole installation. You can keep the app in your Dock. After it opens successfully,
you can delete the downloaded `.dmg` file; the installed app is independent of it.

You do not need **Source code (zip)**, the AI archive or the
third-party source archives to install the app. AudioLab downloads optional AI itself from Settings.

## First opening

This personal beta is **not verified by Apple** (not Developer ID signed or notarized).
macOS may say that Apple cannot check the app or that the developer cannot be verified.
If you trust the download from **JSummerer/AudioLab** and want to run it:

1. Try opening AudioLab from Applications once, then dismiss the warning.
2. Open **System Settings → Privacy & Security** and scroll to **Security**.
3. Find the blocked AudioLab message and choose **Open Anyway**, if offered.
4. Choose **Open** in the confirmation. Enter any administrator approval directly in macOS if asked.

This saves an exception for AudioLab. Wording and availability can vary, especially on a work or
school Mac. Follow [Apple's opening guidance](https://support.apple.com/en-us/102445); changing
system-wide security settings or running Terminal commands is not part of this installation.

If the message says AudioLab **will damage your computer**, was moved to Trash as malware, or is
**damaged**, stop and [report the exact message](#troubleshooting). Those are different from the
expected developer-verification warning.

Once it opens, follow [Your first recording](USAGE.md#your-first-recording). Start with a short
local audio file and Convert or Check; AI setup is optional.

## Optional AI

AI adds **Split** and **Best Enhance**. You can enable it later or remove it without removing the app.

1. Open **Settings → AI & system** and choose **Enable AI features**.
2. Keep AudioLab open and connected to the internet while it downloads. Settings shows the amount
   downloaded, then **Installing** and **Checking** while it unpacks and tests the add-on.
3. Wait for **Ready**. Split and Best Enhance are now available, including both **Standard** and
   **Fine-tuned** model choices. Local audio can then be processed offline.

The download is roughly **0.6 GB**, with about **1 GB** installed. Allow extra free disk space for
installation and audio work. A full download counter is not the end of setup: checking and unpacking
still follow. There is no fixed installation time; it depends on your connection and Mac.

| If you want to… | In Settings → AI & system |
|---|---|
| Stop the installation | Choose **Cancel install**. A partial download is kept. |
| Continue later | Read any error, then choose **Resume AI download** or **Retry AI install**, whichever appears. |
| Fix an installed add-on | Choose **Repair** to reinstall and check the version used by your app. |
| Free its disk space | Choose **Remove AI add-on** and confirm. This also removes partial downloads, models and AI caches. |

Finish or cancel any running AI job before changing the add-on. Removal preserves your recordings,
Library and general settings. You can enable AI again later. AudioLab manages these extra files
separately from any Python or other tools you may already have installed.

AI needs additional **working memory (RAM)** while processing; the disk figures above are not a RAM
requirement. We provisionally prefer **16 GB or more** for early AI testing, without claiming a minimum. Begin with a
short recording and the **Standard AI model**; Fine-tuned is slower. **Standard Enhance quality**
is a separate choice and works without AI. See [hardware guidance](HARDWARE.md) for more detail.

## Updates

1. Quit AudioLab and download the new app from [GitHub Releases](https://github.com/JSummerer/AudioLab/releases).
2. Open its `.dmg`, drag **AudioLab.app** into **Applications**, and choose **Replace** when Finder asks.
3. Eject the disk image and open the new app. Check **Settings → About** for its version.

Your Library, settings and finished audio normally stay outside the app and are reused. Read the
release notes for any special instructions. You may need the first-opening steps again.
The app does not update itself automatically.

For optional AI, check **Settings → AI & system** after updating the app. **Update AI add-on** appears
when that app needs a different compatible version. Otherwise, an existing compatible installation
is reused. The app's audio/link tools update with the app too.

If you also use a [source installation](INSTALLATION.md#install-from-source), quit one copy before
opening the other; they normally share app data.

## Uninstall

**Just remove the app:** quit AudioLab, then move **AudioLab.app** from Applications to Trash.
This keeps your recordings, Library and settings for a possible reinstall.

**Remove optional AI too:** before quitting, use **Settings → AI & system → Remove AI add-on**.
This removes its software, models, cache and partial downloads, but preserves recordings and Library.

**Remove app data as well:** before quitting, use **Settings → About → Open app data** and **Open logs**.
Check what you want to keep, quit AudioLab, then move those AudioLab folders to Trash.
App data includes **Library records and kept original audio copies**, not just caches.
Deleting it loses those copies and your history/settings. Preserve any audio you want first.

If AudioLab no longer opens, use Finder's **Go → Go to Folder…** for the default locations:

- `~/Library/Application Support/AudioLab` — app data, including optional AI and kept originals.
- `~/Library/Logs/AudioLab` — app logs.

For a custom data location, use the actual folder shown by the app. Its logs are in `logs/` beneath it.
**Your finished audio is yours to keep:** normally in `Music/AudioLab`, or your chosen output folder.
Do not remove it as part of app cleanup. Preserve output first if you chose to save it inside app data.

<details>
<summary>Optional cleanup of macOS-managed files</summary>

macOS may also keep the following items. After quitting, remove only these exact AudioLab items if
present, using Finder's **Go → Go to Folder…**. They can vary or be absent.

| Item | Expected location |
|---|---|
| WebKit website data | `~/Library/WebKit/io.github.jsummerer.audiolab` |
| Native caches | `~/Library/Caches/io.github.jsummerer.audiolab` |
| Native preferences | `~/Library/Preferences/io.github.jsummerer.audiolab.plist` |
| Saved window state | `~/Library/Saved Application State/io.github.jsummerer.audiolab.savedState` |

Do not delete general WebKit, browser, Python or shared cache folders. macOS can retain its own
security, recent-item and system records; uninstalling is not a promise to erase every trace.
Older source installations have [separate cleanup notes](INSTALLATION.md#uninstall).

</details>

## Troubleshooting

| Problem | What to do |
|---|---|
| macOS blocks the first opening | Follow [First opening](#first-opening). If **Open Anyway** is unavailable on a managed Mac, ask its administrator. |
| The app still will not open, or says it is damaged | Check the chip/macOS requirement above. Keep the exact message and [report it](https://github.com/JSummerer/AudioLab/issues/new?template=bug_report.md); do not disable macOS protections. |
| AI download or installation stops | Check your connection and free disk space. Read the message in **AI & system**, then Resume or Retry. If it fails again, include the error in a report. |
| Split or Best is unavailable | Enable AI and wait for **Ready**. If already installed, read the reason and use **Repair**. Version 2.4.0b1 needs an app update first. |
| Storage shows an old AI size after installation | In this beta, switch to another screen and back. **AI & system** shows the installed size; the separate Storage display can lag. |
| The text file in the disk image calls AI “upcoming” | That sentence predates publication. **2.4.0b2 includes the Settings installer** described above. |
| A link fails | Check for a newer app release, or use a local audio file. Supported sites can change. |
| A job needs too much memory or disk | Try a shorter recording or Light/Standard Enhance. |

For a failed job, choose **Copy details for a bug report**. For other problems, use
**Settings → About → Copy system report**. If the app cannot open, just report the warning, Mac chip,
macOS version and downloaded file name. Use [GitHub Issues](https://github.com/JSummerer/AudioLab/issues/new?template=bug_report.md)
and describe the steps that led to the problem. Review reports before sharing: names, paths and links
may be personal. Never share passwords, cookies or access tokens.

## Files and privacy

Audio is processed on your Mac. Web links need network access to their source; enabling AI downloads
its software and both models. The app uses a local server for its own window.

| Data | Default location |
|---|---|
| Finished audio and saved covers | `~/Music/AudioLab`, or your choice in **Settings → Files** |
| Settings, jobs, Library and kept originals | `~/Library/Application Support/AudioLab` |
| Audio-tool caches and temporary runtime files | The app-data folder's `cache/` subfolder |
| Optional AI software, models, caches and partial downloads | The app-data folder's `ai/` subfolder |
| App logs | `~/Library/Logs/AudioLab` |

**Settings → About** opens the actual app-data and log folders. Working data includes uploaded copies,
processing intermediates and Check reports. Kept originals support full-length comparison and live
separately in `library/originals/` under app data. Download a separate copy of any Check report you
want to retain after clearing jobs.

**Settings → Storage** can clear finished jobs or explicitly delete kept originals. Removing a Library
entry preserves output files; its kept original may go when no other entry uses it, as the dialog
explains. Copies saved through a download dialog stay wherever you saved them.

For what was tested and remaining beta limitations, see the
[release notes](https://github.com/JSummerer/AudioLab/releases/tag/v2.4.0b2).
Build contributors: see the [macOS packaging guide](../packaging/macos/README.md).
