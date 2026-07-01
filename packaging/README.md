# packaging/ — Headroom Desktop sidecar factory

This directory (plus `cloud/` and the three desktop workflows in
`.github/workflows/`) is the **additive** Headroom Desktop layer on top of the
upstream headroom mirror. Everything else in the repo tracks
`upstream/main` (https://github.com/headroomlabs-ai/headroom) and merges clean —
keep it that way: no edits to the `headroom/` package unless they're
feature-flagged and destined for an upstream PR.

## sidecar/

Builds `headroom-runtime`, the PyInstaller `--onedir` binary bundled inside the
Headroom Desktop app (repo `pedr0v/headroom-desktop`):

- `entry.py` — entrypoint with two subcommands:
  - `setup`: pre-downloads/warms all model weights into `HF_HOME`, emitting one
    JSON progress object per stdout line (consumed by the app's first-run wizard).
  - `proxy`: delegates to `headroom proxy` (the real FastAPI compression proxy).
- `headroom-runtime.spec` — PyInstaller 6 spec (collects `headroom` incl. the
  `_core` Rust extension, magika model data, onnxruntime, tokenizers, metadata).
- `build.sh` — venv build → `dist/headroom-runtime/` →
  `dist/headroom-runtime-v$(cat VERSION)-aarch64-apple-darwin.tar.gz`.
- `smoke_test.py` — boots the runtime against `mock_upstream.py` and asserts:
  `/livez` healthy, completion round-trip, **compression actually observed**
  (the mock records what the proxy forwarded), SSE pass-through, `/stats`.
  Model downloads are cached across runs in `.smoke-cache/` (override:
  `HEADROOM_SMOKE_CACHE`).
- `sign-macos.sh` — signs every Mach-O in the onedir innermost-first
  (hardened runtime); run before the app bundles the sidecar. See the desktop
  repo's `packaging/README.md` for the full signing runbook.

```sh
cd packaging/sidecar
python3.12 -m venv .venv && .venv/bin/pip install 'headroom-ai[proxy]' pyinstaller
bash build.sh
.venv/bin/python smoke_test.py dist/headroom-runtime/headroom-runtime
```

Payload shape matters in the smoke test: headroom compresses the *live zone* of
agent traffic (first-sight `role:"tool"` results, JSON → SmartCrusher). Plain
user messages are deliberately protected as prefix-cache-stable, so a naive
"big user message" payload shows zero savings — that's by design, not a bug.

Releases: tag `sidecar-v*` → `.github/workflows/sidecar-release.yml` builds and
uploads the tarball + sha256; the desktop repo pins it in
`scripts/sidecar-version.json`.

## cloud/

Supabase project for the SaaS layer (auth, append-only conversation sync,
Stripe billing, numbers-only savings telemetry, feature flags). See
`cloud/README.md` for the deploy runbook.
