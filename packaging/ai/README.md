# Optional macOS AI pack

This maintainer build produces an independent, directory-style frozen helper
for Apple silicon and macOS 14 or newer. The end user needs neither Python nor
Terminal. The core app downloads the exact compatible archive from its embedded
catalog; the helper contains both supported model choices and works offline.

Core app builds reuse the reviewed, immutable **AI 1.0.0** from `v2.4.0b2` by
default. `published-helper.json` pins its original catalog, runtime archive and
source companion. `reuse.py` downloads and verifies those exact assets, then runs
the full helper smoke check. Set `AUDIOLAB_AI_REUSE_DIR` to a retained release
directory containing `ai-catalog.json` and both AI archives to avoid downloading
them on the build machine. The embedded URL and receipt identity stay unchanged,
so a core-only update also reuses the user's already installed helper.

Reuse checks 22 execution/contract and build inputs against hashes derived from
that exact source companion, and checks the companion against its own asset pin.
The source closure is `worker`, `separation`, `ai/__init__`, `context`, `errors`,
`system`, `runtime`, app constants, and the core client's helper protocol. Only
the root package's literal `__version__` value and text checkout line endings are normalized. Model/dependency
pins, entry, freezer spec and payload preparation/archive recipes are checked.
Adding an import to a guarded module changes its hash and refuses reuse.

The worker entry sets its role before importing AudioLab: managed installation,
source-model downloads, core runtime setup and desktop functions are dormant
branches, not worker execution dependencies. `managed.py`, core web assets and
app-only modules are outside this boundary. Current smoke tests and build/reuse
launchers are orchestration, not frozen inputs; the original launcher hash is
retained as provenance. Changing this boundary needs review. A mismatch stops the
build; it never silently trusts a matching version or rebuilds the AI pack.
Reused archives stay at their original public release, including the matching
source companion; the new core release includes only the unchanged AI catalog.
Keep that original release available. The build work folder retains both archives
for the helper and core integration smoke checks, without uploading them again.

To intentionally build a **new** helper, choose a new AI version (the published
version is refused), then use the following standalone build or set
`AUDIOLAB_AI_MODE=rebuild` with that version for the core build:

Use the same Python Build Standalone **3.12.15, build 20261001** selected by the
core macOS workflow, Xcode Command Line Tools, and uv. No global Python packages
or user model caches are used by the build. Use a fresh owned work directory for
each prepared release:

```sh
AUDIOLAB_BUILD_PYTHON=/path/to/pinned/python \
AUDIOLAB_AI_WORK_DIR="$PWD/.work/ai-package" \
AUDIOLAB_AI_VERSION=1.1.0 \
bash packaging/ai/build.sh
```

`AUDIOLAB_AI_VERSION` is required for a rebuild; it describes the helper protocol,
dependencies and models independently of the app version. The release tag defaults
to `ai-v<AI version>`; `AUDIOLAB_AI_RELEASE_TAG` can explicitly select another tag.
An existing prepared archive is never replaced silently. After validating and
publishing the new helper, update `published-helper.json` from its exact catalog,
archive checksums and matching source companion, and review its execution closure.
Keep the old release assets immutable. Do not generate a new baseline solely from
the current checkout or relabel rebuilt bytes as an existing helper version.

The `release/` folder contains the runtime `.tar.gz`, exact catalog, native and
import inventory, source companion and checksums. The core builder must embed
that catalog before freezing the app, then publish the runtime archive at the
exact catalog URL together with its matching source companion. The source
companion is a developer asset; users install only the runtime archive.

The archive contains `AudioLabAI`, `_internal/`, `models/`, `pack.json`,
`ThirdParty/`, and the AudioLab license directly at its root. PyInstaller's
six pinned relative library aliases are archived as standard tar hardlinks to
their direct regular-file members. Every other member is a directory or regular
file; runtime symlinks and link chains are rejected. The extractor writes all
regular payload before creating the checked hardlinks. `installed_bytes` is the
exact sum of regular member content lengths, so each native library is stored
once. These aliases share file content and permissions with their targets.

`requirements.lock` pins the complete build environment. The actual frozen
import inventory is smaller: unused Hugging Face networking, codecs, training
extras and UI/server modules are excluded. `models.json` pins the upstream
commit, size and SHA256 of each original YAML and safetensors file. Standard
uses one 84,025,440-byte model; Fine-tuned uses four of the same size. The model
files total **420,127,370 bytes**, including YAML. Runtime loading uses only
these installed files, never generic remote model resolution.

`prepare.py` retains original wheel notices, the reviewed Python runtime
notices, matching SoundFile/native LGPL and SciPy/GCC sources, exact Python
sdists, and PyTorch source matching the wheel's embedded Git commit. PyTorch's
wheel includes its original native dependency license tree. Safetensors' Rust
closure is documented in [SAFETENSORS-NATIVE.md](SAFETENSORS-NATIVE.md).
The source companion includes the exact AudioLab source snapshot frozen into
the helper and these build recipes. It preserves upstream archives without
claiming that a rebuild is bit-for-bit reproducible.

The build verifies every Mach-O slice and declared minimum OS, each native
ad-hoc signature, actual import ownership, original notices and pinned models.
The archive smoke test extracts the delivered format, removes external Python
from PATH, probes it, separates one second of generated stereo audio with both
models, verifies the hardlink inodes and extracted native signatures, and checks
that a worker retains its inherited operation lock after its parent is killed.
Generated media and evidence remain in the owned work folder.

This is an ad-hoc signed helper, not Developer ID signed or notarized. Local
headless CPU checks do not qualify every macOS version, MPS performance, real
music quality or the manual Gatekeeper interaction.
