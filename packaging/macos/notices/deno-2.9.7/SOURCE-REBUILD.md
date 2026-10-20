# Reconstructing and rebuilding the bundled Deno

The `deno-reconstruction` group in `sources.json` contains Deno 2.9.7, all
registry packages in its upstream Cargo.lock, RustyV8 150.4.0, its pinned native
source dependencies, and Rust 1.95.0 standard-library sources. The native V8
revision is `ac1e23989121713ca642f6650b34deff7b686896`. Original license texts
remain in the sources. This includes the LGPL-2.1 glibc math sources under
`rusty_v8/v8/third_party/glibc/`; modifications there can be compiled into a new
V8 static library and linked into a replacement Deno executable.

Use Python 3.10 or newer. From the extracted source companion, reconstruct a
**new or empty** working directory (choose an absolute output path):

```sh
python3 packaging/macos/restore_deno_sources.py \
  --archives sources \
  --output /absolute/path/to/deno-source-work
```

In an AudioLab source checkout, the helper and manifest are in
`packaging/macos/`; pass the companion's `sources/` directory to `--archives`.
The helper verifies every selected archive's SHA256 before extraction, refuses
unsafe paths or links, restores executable file modes and writes Cargo vendor
checksums. It downloads nothing and executes no upstream build scripts. A
failed extraction may leave a partial directory; use a fresh directory to retry.

The restored `deno/Cargo.lock.upstream` is an unchanged copy of Deno's original
lockfile. The active `deno/Cargo.lock` initially has the same bytes. The helper
adds registry source replacement in `deno/.cargo/config.toml` and a local
`[patch.crates-io]` for `v8` in `deno/Cargo.toml`, pointing to `../rusty_v8`.
Cargo will update the active lockfile for this deliberate local-source patch;
keep the upstream copy to audit that change. The retained source manifest is
`source-manifest.json`.

Build on Apple Silicon macOS with Rust/Cargo **1.95.0**, Xcode Command Line
Tools with a macOS SDK, Python 3, and sufficient RAM and disk for a full V8/Deno
build. Install those tools independently. Deno's macOS arm64 Cargo settings use
`-fuse-ld=lld`, so provide LLVM 20 or later's `ld64.lld` on PATH (the upstream config warns
that LLD 17 fails with its chained-fixup settings).
RustyV8's source binding generator requires a compatible libclang (its build
script specifies Clang 19 or later); set `LIBCLANG_PATH` to that installation's
library directory. A complete compatible LLVM installation can supply both.
Follow the retained upstream files when choosing tool versions.

Run Cargo from inside **`deno/`**: Cargo discovers `.cargo/config.toml` from the
working directory, even when a manifest is passed explicitly.

```sh
cd /absolute/path/to/deno-source-work/deno
cargo --version
rustc --version
cargo metadata --offline --format-version 1 --manifest-path cli/Cargo.toml > ../cargo-metadata.json
LIBCLANG_PATH=/absolute/path/to/llvm/lib \
  V8_FROM_SOURCE=1 cargo build --release --bin deno --manifest-path cli/Cargo.toml
./target/release/deno --version
```

`V8_FROM_SOURCE=1` is the switch used by this exact RustyV8 `build.rs`: it runs
`build_v8` and `build_binding`, rather than downloading the prebuilt V8 library.
Its source build obtains GN/Ninja if absent, invokes `tools/rust_toolchain.py`
to obtain its pinned native-build Rust toolchain, and downloads Chromium clang
unless `CLANG_BASE_PATH` identifies an existing suitable installation (the
prefix containing `bin/clang`). `GN`, `NINJA`, `PYTHON`, `CLANG_BASE_PATH` and
`GN_ARGS` are supported overrides in that build script. Inspect
`rusty_v8/build.rs`, `rusty_v8/tools/rust_toolchain.py`, and
`rusty_v8/tools/v8_deps.py` for exact acquisition behavior. The retained
`rust-toolchain-source/` tree contains standard-library source; it is not an
installed compiler or a substitute for the native-build toolchain.

The metadata command can resolve registry packages offline from the retained
vendor tree. **This does not make native compilation fully offline.** Native
build tools, the SDK, compilers and their own downloaded components must be
available separately. Source restoration and Cargo metadata resolution were
checked for this source set; a complete V8/Deno native rebuild was not run.
The recipe is a source modification and relinking route, not a claim of
bit-identical output, a finished replacement binary, or qualification on every
macOS/toolchain combination.

After modifying the glibc sources or other native sources, use a clean build
directory or `cargo clean` in this reconstructed tree before the command above
so an existing native archive cannot hide the modification. The resulting
`deno/target/release/deno` is a standalone replacement for the bundled Deno
helper. AudioLab's packaging verifies the official helper's original hash, so
an intentionally modified executable needs corresponding source changes:

1. In a fresh AudioLab source build environment, locate the installed helper
   with that environment's Python: `python -c "import deno; print(deno.find_deno_bin())"`.
   Replace that **build environment's** helper with your rebuilt executable.
2. Record its SHA256 and changes in the Deno notice `PROVENANCE.json`, and
   update `DENO_SHA256` in `packaging/macos/bundle_inventory.py` to that SHA256.
   Keep original notices and add notices for any newly introduced dependencies.
   This deliberate source change distinguishes your modified build from the
   official release's verified input.
3. Run `packaging/macos/build.sh` with the pinned build prerequisites described
   in its README. Its notice checks, freezing, signing and smoke checks use
   the chosen helper. Replacing a helper inside an already signed app without
   rebuilding invalidates that signature.

Keep your source changes and replacement executable's license notices with the
rebuilt app. This is a rebuild of open-source components; no proprietary AudioLab
object files are required for relinking.
