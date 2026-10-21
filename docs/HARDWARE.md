# Hardware and storage

For the **AudioLab 2.4.0b3 Mac beta**. [Download and install](MACOS.md) ·
[Memory](#memory-ram) · [Disk space](#disk-space) · [Speed and models](#speed-and-models) ·
[Checks and limits](#what-the-app-checks)

## What do I need?

| Item | Core tools: Save / Convert, Check, Light/Standard Enhance | Optional AI: Split and Best Enhance |
|---|---|---|
| Mac and system | Apple silicon, macOS 14 or later | Same; uses the built-in Apple GPU when available |
| Memory guidance, provisional | **Already have 8 GB?** Start with short core tasks; this capacity has not been qualified | For early AI testing, we prefer existing Macs with **16 GB or more**; capacity on 8/16 GB has not yet been tested |
| Free disk space for short jobs, planning allowance | Aim for **5 GB free** after installing the app | Aim for **10 GB free** after installing the app and AI add-on |
| Internet | Needed for downloads and web links; local files can be processed offline | Needed to install AI; both included model choices then work offline |

**The memory and free-space figures are planning recommendations, not tested minimums or guarantees.**
There is no qualified minimum RAM capacity yet. “Short” here means a recording of a few minutes,
up to roughly 10 minutes, one job at a time. Longer recordings need more space and often more memory.
Do not read the table as support for hour-long processing on an 8 GB Mac.
This helps you try hardware you already own; it is **not a reason to buy or upgrade a Mac** for this beta.
Find your installed memory in **Apple menu → About This Mac**.

The packaged app requires no separate graphics card, Python installation or Apple Intelligence setup.
It has no published CPU-core or clock-speed requirement. Intel Macs, Windows and Linux do not have
packaged releases; their [source installation](INSTALLATION.md#install-from-source) is a separate,
experimental route.

## Memory (RAM)

RAM is the memory used **while working**. The roughly 1 GB AI installation is disk storage; it does
not mean AI needs only 1 GB RAM. On Apple silicon, CPU and GPU share the computer's memory pool.
[Apple explains this architecture](https://developer.apple.com/videos/play/wwdc2020/10686/).

- **Already have 8 GB?** Begin with a short file using Convert, Check or Light Enhance. Try more
  demanding work gradually; AI and long Enhance jobs are not qualified on an 8 GB machine.
- **16 GB or more:** our provisional preference for early AI testing with short files. It offers
  more headroom than 8 GB, but this capacity and longer Enhance jobs remain unqualified.
- **Long mixes or many other open apps:** more RAM gives additional headroom. There is no tested
  24/32 GB tier or promised maximum duration; shorter recordings remain the most direct way to
  reduce a job's working data.

Standard Enhance also keeps full-length audio arrays in memory. Some filters and AI stages work in
chunks, but the complete Enhance job does not use a fixed amount of RAM regardless of duration.
The compressed MP3/FLAC file size is therefore a poor guide to processing memory.

If the Mac becomes sluggish, cancel the job, close other demanding apps and retry a shorter file or
use Light Enhance. You can inspect **Activity Monitor → Memory → Memory Pressure** if needed;
[Apple's guide](https://support.apple.com/guide/activity-monitor/view-memory-usage-actmntr1004/mac)
explains that display. This is optional troubleshooting, not an installation requirement.

## Disk space

Measured payloads for this release, rounded in decimal MB/GB:

| Files | Download | Installed files |
|---|---:|---:|
| Main Mac app | 106 MB | About 206 MB |
| Optional AI, including both models | 570 MB | About 1.02 GB |

These exclude recordings, Library originals, working audio, caches and filesystem overhead.
The AI installer needs space for the download and unpacked files at the same time. A fresh install's
current free-space check is about **1.69 GB additional space**, but passing it only covers installation.
Keep the larger working-space allowance in the table above for actual use. Repair/update may keep
an existing installation while preparing its replacement.

Temporary audio can be much larger than the original file. The app checks a task/duration estimate
plus a 2 GiB allowance before processing. Rounded **current check thresholds for one 60-minute job** are:

| Task | Free-space estimate, including that allowance |
|---|---:|
| Save / Convert | About 4.0 GB |
| Check | About 4.6 GB |
| Light/Standard Enhance | About 8.9 GB |
| Best Enhance | About 16.4 GB |
| Split | About 17.4 GB |

These are calculations from the current code, **not measured peak disk use or reserved capacity**.
The estimate does not cover every source sample rate, export format or combination of outputs; even
the 2 GiB allowance is not guaranteed to remain free after a job. Leave additional room
for your existing and new recordings, kept originals, other apps and macOS. If work and output folders
are on different drives, the app checks both drives. Choosing MP3 output does not remove the need for
uncompressed working files during Enhance or Split.

Use **Settings → Storage** for finished working data, and **AI & system → Remove AI add-on** for the
optional AI files. Kept originals are valuable audio and have a separate deletion control. See
[files and cleanup](MACOS.md#files-and-privacy) before removing anything.

## Speed and models

AI uses the Apple GPU through PyTorch's MPS backend when available; **Settings → AI & system** shows
what was detected. Otherwise it can use the CPU. No extra graphics driver installation is part of
this Mac beta. More memory primarily gives work room to fit; it is not a promise of proportionally
faster processing.

Start with the **Standard AI model**. Fine-tuned combines four models and generally takes longer;
the [Demucs author's guide](https://github.com/adefossez/demucs#separating-tracks) describes roughly
four times the work time. That is upstream guidance, not an AudioLab timing guarantee. Fine-tuned
also loads more model weights, but its total peak RAM is not necessarily four times higher.
Choosing vocals/instrumental instead of four stems still performs the underlying separation.

**Standard AI model** and **Standard Enhance quality** are different choices: Standard Enhance
works without AI. Best Enhance uses AI in addition to the ordinary processing chain.

CPU-only AI is limited to **20 minutes of input per job by default**. This is an app policy to avoid
very long CPU jobs, not a hardware minimum or a runtime estimate. There is no reliable
“minutes per song” promise yet. Use a shorter file or Light/Standard Enhance when resources are tight.

## What the app checks

The app already estimates memory, working/output disk and recording duration before heavy work.
However, on a normal Mac its memory budget uses **85% of installed RAM**, not a live measurement of
memory left after other apps. It does not account separately for each AI model or all GPU allocations.
Passing that check is not a guarantee against memory pressure or an out-of-memory error.

For example, the current formula estimates about **6.84 GiB** for one hour of Standard Enhance.
That is above the **6.8 GiB** default budget on an 8 GiB Mac, so the app refuses that job even with
other apps closed. These are formula values, not a new measured benchmark. Closing apps can improve
actual headroom, but does not raise this installed-RAM budget.

We are keeping those limits unchanged while documenting their scope. The app does not reject every
AI request solely because a Mac has 8 GB, and it does not certify all 16 GB jobs as safe.

## What has been tested

The b3 app has passed short generated-audio core and AI checks, reuse/removal of an existing AI
installation, and native-window checks. Both AI model choices passed helper checks. Public-download
installation/cancel/resume was checked on b2 with this same AI pack. Native-window validation ran on
macOS 15.7.9 arm64; local functional checks used an Apple M4 Pro with 48 GB on macOS 26.6.2.
**That is a test configuration, not a 48 GB requirement.** macOS 14 is the build target and has not been tested natively.
See the [release notes](https://github.com/JSummerer/AudioLab/releases/tag/v2.4.0b3) for the exact scope.

This evidence does not qualify minimum RAM, long-recording performance or every chip. A supported
minimum needs checks on actual lower-memory Macs, including the app and AI helper together, GPU/system
memory pressure, model choice, audio duration and peak working disk. Restricting a process on a
48 GB Mac would not reproduce an 8 GB Mac's unified-memory behavior. Until those checks exist,
**8 GB / 16 GB remain guidance**, and the beta stays usable without a new hardware gate.
