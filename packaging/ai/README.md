# Optional macOS AI pack

This maintainer build produces an independent, directory-style frozen helper
for Apple silicon and macOS 14 or newer. The end user needs neither Python nor
Terminal. The core app downloads the exact compatible archive from its embedded
catalog; the helper contains both supported model choices and works offline.

Use the same Python Build Standalone **3.12.15, build 20261001** selected by the
core macOS workflow, Xcode Command Line Tools, and uv. No global Python packages
or user model caches are used by the build. Use a fresh owned work directory for
each prepared release:

```sh
AUDIOLAB_BUILD_PYTHON=/path/to/pinned/python \
AUDIOLAB_AI_WORK_DIR="$PWD/.work/ai-package" \
AUDIOLAB_AI_RELEASE_TAG=v2.4.0b2 \
bash packaging/ai/build.sh
```

`AUDIOLAB_AI_VERSION` defaults to `1.0.0`; it describes the helper protocol,
dependencies and models, independently of the app version. The release tag
defaults to `ai-v<AI version>` if the maintained workflow does not supply its app
release tag. An existing prepared archive is never replaced silently.

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
