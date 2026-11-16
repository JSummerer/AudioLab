# Hardware and storage

For the **AudioLab 2.5.0 Mac beta**. [Download and install](MACOS.md) ·
[Memory](#memory-ram) · [Disk space](#disk-space) · [Speed and models](#speed-and-models) ·
[Checks and limits](#what-the-app-checks)

## What do I need?

| Item | Core tools: Save / Convert, Check, Volume only/Adaptive Enhance | Optional AI: Split and AI stems Enhance |
|---|---|---|
| Mac and system | Apple silicon, macOS 14 or later | Same; uses the built-in Apple GPU when available |
| Memory guidance, provisional | **Already have 8 GB?** Start with short core tasks; this capacity has not been qualified | For early AI testing, we prefer existing Macs with **16 GB or more**; capacity on 8/16 GB has not yet been tested |
| Free disk space for short jobs, planning allowance | Aim for **5 GB free** after installing the app | Aim for **10 GB free** after installing the app and AI add-on |
| Internet | Needed for downloads and web links; local files can be processed offline | Needed to install AI; both included model choices then work offline |

**The memory and free-space figures are planning recommendations, not tested minimums or guarantees.**
There is no qualified minimum RAM capacity yet. “Short” here means a recording of a few minutes,
up to roughly 10 minutes, one job at a time. Longer recordings need more space and often more memory.
The memory check now lets Volume only and Adaptive Enhance start on long recordings on an 8 GB Mac.
On a larger Mac a one-hour job used about 1.2 GB of working memory plus working files on disk, but no
long job has been tested on an 8 GB machine yet.
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

- **Already have 8 GB?** Begin with a short file using Convert, Check or Volume only Enhance. Try more
  demanding work gradually; AI and long Enhance jobs are not qualified on an 8 GB machine.
- **16 GB or more:** our provisional preference for early AI testing with short files. It offers
  more headroom than 8 GB, but this capacity and longer Enhance jobs remain unqualified.
- **Long mixes or many other open apps:** more RAM gives additional headroom. There is no tested
  24/32 GB tier or promised maximum duration; shorter recordings remain the most direct way to
  reduce a job's working data.

Enhance uses disk-backed working buffers beyond 10 minutes and processes more stages in blocks.
This reduces the need to allocate full-length audio arrays in memory. Working memory still depends
on the recording's duration, the data mapped from disk and any AI processing. Compressed MP3 or FLAC
size is a poor guide to processing memory.

If the Mac becomes sluggish, cancel the job, close other demanding apps and retry a shorter file or
use Volume only Enhance. You can inspect **Activity Monitor → Memory → Memory Pressure** if needed;
[Apple's guide](https://support.apple.com/guide/activity-monitor/view-memory-usage-actmntr1004/mac)
explains that display. This is optional troubleshooting, not an installation requirement.

## Disk space

Reference payloads measured on 2.4.0b3, rounded in decimal MB/GB. See the
[latest release](https://github.com/JSummerer/AudioLab/releases/latest) for the current app's download size.
2.5.0 uses the same AI 1.0.0 payload.

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
plus a 2 GiB allowance before processing. The table shows rounded **free-space thresholds for one
60-minute job** in decimal GB. These disk figures are unchanged since 2.4.0b6; only the memory estimate
changed in 2.5.0 (see [what the app checks](#what-the-app-checks)).

| Task | Free-space estimate, including that allowance |
|---|---:|
| Save / Convert | About 4.0 GB |
| Check | About 4.6 GB |
| Volume only/Adaptive Enhance | About 15.1 GB |
| AI stems Enhance | About 22.6 GB |
| Split | About 17.4 GB |

The Enhance estimates include up to **6.2 GB (5.8 GiB) per hour** for disk-backed working buffers
on recordings longer than 10 minutes.

These figures are the app's preflight estimates.
The estimate does not cover every source sample rate, export format or combination of outputs; even
the 2 GiB allowance is not guaranteed to remain free after a job. Leave additional room
for your existing and new recordings, kept originals, other apps and macOS. The estimates above are for
the drive with app data, where the working files go. The output folder is checked separately: for Enhance
and Split it needs room only for the results, but for Save / Convert the larger working-space figure above
still applies there (Check writes nothing there). Choosing MP3 output does not remove the need for
uncompressed working files during Enhance or Split.

Use **Settings → Storage → Clear finished jobs** for the files of finished jobs, and
**Settings → Updates & add-ons → Remove AI add-on** for the optional AI files. Kept originals are valuable audio and have a separate deletion control. See
[files and cleanup](MACOS.md#files-and-privacy) before removing anything.

## Speed and models

AI uses the Apple GPU through PyTorch's MPS backend when available; **Settings → Updates & add-ons** shows
what was detected. Otherwise it can use the CPU. No extra graphics driver installation is part of
this Mac beta. More memory primarily gives work room to fit; it is not a promise of proportionally
faster processing.

Start with the **Standard AI model**. Fine-tuned combines four models and generally takes longer;
the [Demucs author's guide](https://github.com/adefossez/demucs#separating-tracks) describes roughly
four times the work time. That is upstream guidance, not an AudioLab timing guarantee. Fine-tuned
also loads more model weights, but its total peak RAM is not necessarily four times higher.
Choosing vocals/instrumental instead of four stems still performs the underlying separation.

**Standard AI model** and **Adaptive processing** are different choices: Adaptive Enhance
works without AI. AI stems Enhance uses AI in addition to the ordinary processing chain.

CPU-only AI is limited to **20 minutes of input per job by default**. This is an app policy to avoid
very long CPU jobs, not a hardware minimum or a runtime estimate. There is no reliable
“minutes per song” promise yet. Use a shorter file or Volume only/Adaptive Enhance when resources are tight.

## What the app checks

The app already estimates memory, working/output disk and recording duration before heavy work.
However, on a normal Mac its memory budget uses **85% of installed RAM**, not a live measurement of
memory left after other apps. It does not account separately for each AI model or all GPU allocations.
Passing that check is not a guarantee against memory pressure or an out-of-memory error.

For example, the formula now estimates about **2.64 GiB** for one hour of Adaptive Enhance (6.84 GiB
before), inside the **6.8 GiB** default budget on an 8 GiB Mac. Beyond 10 minutes, Volume only and
Adaptive Enhance work in files on disk, so their estimate grows by only 0.5 GiB per hour (about 6.14 GiB
at 8 hours). AI stems Enhance keeps the earlier formula (about 7.84 GiB for one hour). These are formula
values based on measurements on one 48 GB Mac, not a tested minimum. Closing apps can improve actual
headroom, but does not raise this installed-RAM budget. The estimate assumes the app data folder is on a
real disk: on a RAM disk (tmpfs, as in some container setups) the working files of a long Enhance job take
memory too, AudioLab does not detect that, and such a job can run out of memory.

The AI estimates are unchanged. The app does not reject every
AI request solely because a Mac has 8 GB, and it does not certify all 16 GB jobs as safe.

## What has been tested

The 2.4.0b3 app passed short generated-audio core and AI checks, reuse/removal of an existing AI
installation, and native-window checks. Both AI model choices passed helper checks. Public-download
installation/cancel/resume was checked on 2.4.0b2 with this same AI pack. Native-window validation ran on
macOS 15.7.9 arm64; local functional checks used an Apple M4 Pro with 48 GB on macOS 26.6.2.
**That is a test configuration, not a 48 GB requirement.** macOS 14 is the build target and has not been tested natively.
The [latest release notes](https://github.com/JSummerer/AudioLab/releases/latest) describe the current
build's test scope.

This evidence does not qualify minimum RAM, long-recording performance or every chip. A supported
minimum needs checks on actual lower-memory Macs, including the app and AI helper together, GPU/system
memory pressure, model choice, audio duration and peak working disk. Restricting a process on a
48 GB Mac would not reproduce an 8 GB Mac's unified-memory behavior. Until those checks exist,
**8 GB / 16 GB remain guidance**, and the beta stays usable without a new hardware gate.
