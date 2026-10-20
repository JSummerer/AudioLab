# Safetensors 0.8.0 native provenance

The arm64 wheel contains a Rust extension. Its original top-level Apache license
is retained unchanged, alongside original notices from its locked dependencies.

The exact PyPI sdist in `sources.json` contains `bindings/python/Cargo.lock`.
The reviewed production and build dependency graph was resolved with:

```sh
cargo metadata --locked --filter-platform aarch64-apple-darwin --format-version 1 \
  --manifest-path bindings/python/Cargo.toml
```

Starting from `safetensors-python`, retain normal/build edges and exclude dev
edges. This yields the two local packages from the sdist and **39 registry
crates**, including procedural macros and build helpers. Each registry archive
is pinned to its Cargo.lock checksum in `sources.json`; versions and upstream
license expressions are recorded in `safetensors-native.json`. Build-only
notices are retained conservatively. The source companion includes all these
unmodified crate archives. Build and release checks require their individual
original license/notice files in `ThirdParty/safetensors-native/`.

Six objc2-family crate archives contain no original license text. Their embedded
`.cargo_vcs_info.json` records identify four precise monorepo revisions. The
unmodified source archives for those revisions are also pinned in `sources.json`;
the builder copies their original license trees into each affected crate's
notice folder. Those overrides preserve source custody instead of substituting
a generic license template.

The wheel's native extension embeds Rust source paths naming compiler commit
`59807616e1fa2540724bfbac14d7976d7e4a3860`, which identifies Rust **1.95.0**.
The release checker verifies that exact embedded identity after freezing.
The Rust 1.95.0 standard-library source archive is pinned in `sources.json`.
Original standard-library notices and its generated copyright inventory are
the same reviewed 1.95.0 texts already retained by the core's Deno packaging.
Those original files are copied into the AI helper as well.

This establishes source and notice custody for the selected wheel. It does not
claim independent reproduction of upstream compiler flags or binary bytes.
