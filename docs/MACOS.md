# AudioLab for Mac

**[Download the latest release](https://github.com/JSummerer/AudioLab/releases/latest)** (AudioLab 2.5.1) · public beta

No Terminal, Python installation or AudioLab account is needed. The app contains its own audio tools.
Save / Convert, Check, the Library, and Enhance in **Volume only** or **Tone and volume** mode work
without the optional AI download.

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

1. **Choose your download:** open the latest release page above and select the `.dmg` under **Assets**.
   The file is named like **AudioLab-2.5.1-macOS-arm64.dmg** and normally appears in Downloads.
2. **Open the downloaded file.** In its window, drag **AudioLab.app** into **Applications**.
   Wait for Finder to finish copying, then eject the AudioLab disk image in Finder's sidebar.
3. **Open AudioLab from Applications.** If macOS blocks it, follow the next section.

That's the whole installation. You can keep the app in your Dock. After it opens successfully,
you can delete the downloaded `.dmg` file; the installed app is independent of it.

You do not need **Source code (zip)**, the AI archive or the
third-party source archives to install the app. AudioLab downloads optional AI itself from Settings.

## First opening

This beta is **not verified by Apple** (not Developer ID signed or notarized).
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

AI adds **Split** and the **AI stems** mode in Enhance. You can enable or remove the add-on in Settings.

1. Open **Settings → Updates & add-ons** and choose **Enable AI features**.
2. Keep AudioLab open and connected to the internet while it downloads. Settings shows the amount
   downloaded, then **Installing** and **Checking** while it unpacks and tests the add-on.
3. Wait for **Ready**. Split and AI stems are now available, including both **Standard** and
   **Fine-tuned** model choices. Local audio can then be processed offline.

The download is roughly **0.6 GB**, with about **1 GB** installed. Allow extra free disk space for
installation and audio work. A full download counter is not the end of setup: checking and unpacking
still follow. There is no fixed installation time; it depends on your connection and Mac.

| If you want to… | In Settings → Updates & add-ons |
|---|---|
| Stop the installation | Choose **Cancel install**. A partial download is kept. |
| Continue later | Read any error, then choose **Resume AI download** or **Retry AI install**, whichever appears. |
| Fix a broken add-on | **Repair AI add-on** appears only when the add-on is broken. It reinstalls and checks the version used by your app. |
| Free its disk space | Choose **Remove AI add-on** and confirm. This also removes partial downloads, models and AI caches. |

Finish or cancel any running AI job before changing the add-on. Removal preserves your recordings,
Library and general settings. You can enable AI again later. AudioLab manages these extra files
separately from any Python or other tools you may already have installed.

AI needs additional **working memory (RAM)** while processing; the disk figures above are not a RAM
requirement. We provisionally prefer **16 GB or more** for early AI testing, without claiming a minimum. Begin with a
short recording and the **Standard AI model**. Fine-tuned is slower. See [hardware guidance](HARDWARE.md).

## Updates

**Settings → Updates & add-ons → Check now** shows when a newer AudioLab exists and links to its
release page; nothing is downloaded or installed. With **Check for updates automatically** on (off by
default), AudioLab checks once a day while it is open. This check exists from 2.5.0 on: an older build
(2.4.0b1–b6) is updated by downloading the new `.dmg` once, as below. To update, replace the app yourself:

1. Quit AudioLab and download a fresh `.dmg` from the [latest release](https://github.com/JSummerer/AudioLab/releases/latest).
2. Open the `.dmg` and drag **AudioLab.app** into the same **Applications** folder as your existing
   copy. Choose **Replace** when Finder asks.
3. Eject the disk image and open the new app from that folder. Confirm the new version in **Settings → About**.

Your Library, settings and finished audio are stored outside the app and carry over when you replace
it. macOS asks again for each new copy: follow the [first-opening steps](#first-opening) (**Open Anyway**).

The downloader for web links (yt-dlp) updates inside the app. When links start failing, choose
**Update downloader** in **Settings → Updates & add-ons**; a job that failed because the site changed
offers the same button. AudioLab never updates it by itself. It fetches yt-dlp's newest stable release
from GitHub, checks its size, checksum and that it starts, and keeps the copy built into the app as the
fallback: **Use built-in version** goes back to it while an update is in use or after one failed to
start. A newer app's built-in downloader is used whenever it is newer than your update. The audio
engine (ffmpeg) updates only with the app.

AudioLab 2.5.1 reuses an existing **AI 1.0.0** installation (unchanged since 2.4.0b2). You can check its status in
**Settings → Updates & add-ons**. **Update AI add-on** appears when an app version needs a different add-on.

If you also use a [source installation](INSTALLATION.md#install-from-source), quit one copy before
opening the other; they normally share app data.

## Uninstall

**Just remove the app:** quit AudioLab, then move **AudioLab.app** from Applications to Trash.
This keeps your recordings, Library and settings for a possible reinstall.

**Remove optional AI too:** before quitting, use **Settings → Updates & add-ons → Remove AI add-on**.
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
Do not remove it as part of app cleanup. Current versions refuse an output folder inside app data; if
an older version saved output there, preserve it first.

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
| AI download or installation stops | Check your connection and free disk space. Read the message in **Updates & add-ons**, then Resume or Retry. If it fails again, include the error in a report. |
| Split or AI stems is unavailable | Enable AI and wait for **Ready**. If already installed, read the reason and use **Repair AI add-on** when it appears. Version 2.4.0b1 needs an app update first. |
| A link fails | Choose **Update downloader** (offered on the failed job, or in **Settings → Updates & add-ons**), or use a local audio file. Supported sites can change. |
| A job needs too much memory or disk | Try a shorter recording or switch to "Volume only" or "Tone and volume" in Enhance. |

For a failed job, choose **Copy details for a bug report**. For other problems, use
**Settings → About → Copy system report**. If the app cannot open, just report the warning, Mac chip,
macOS version and downloaded file name. Use [GitHub Issues](https://github.com/JSummerer/AudioLab/issues/new?template=bug_report.md)
and describe the steps that led to the problem. Review reports before sharing: names, paths and links
may be personal. Never share passwords, cookies or access tokens.

## Files and privacy

Audio is processed on your Mac. Web links need network access to their source. AudioLab contacts
GitHub when you click **Check now** or **Update downloader**, or install the AI add-on (its software and
both models); with **Check for updates automatically** on, it also checks once a day. GitHub then
sees your IP address; nothing else is sent. The app uses a local server for its own window.

| Data | Default location |
|---|---|
| Finished audio and saved covers | `~/Music/AudioLab`, or your choice in **Settings → Files** |
| Settings, jobs, Library and kept originals | `~/Library/Application Support/AudioLab` |
| Audio-tool caches and temporary runtime files | The app-data folder's `cache/` subfolder |
| Downloader updates | The app-data folder's `downloader/` subfolder |
| Optional AI software, models, caches and partial downloads | The app-data folder's `ai/` subfolder |
| App logs | `~/Library/Logs/AudioLab` |

**Settings → About** opens the actual app-data and log folders. Job files in app data include uploaded
copies, processing intermediates and Check reports. Kept originals support full-length comparison and
Adjust, and live separately in `library/originals/` under app data. Download a separate copy of any Check report you
want to retain after clearing jobs.

**Settings → Storage** can clear finished jobs or explicitly delete kept originals. Removing a Library
entry keeps output files unless you choose to move them to the Trash; its kept original may go when no
other entry uses it, as the dialog explains. Copies saved through a download dialog stay wherever you saved them.

For what was tested and remaining beta limitations, see the
[release notes](https://github.com/JSummerer/AudioLab/releases/latest).
Build contributors: see the [macOS packaging guide](../packaging/macos/README.md).
