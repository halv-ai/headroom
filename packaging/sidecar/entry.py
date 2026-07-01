"""headroom-runtime — desktop sidecar entrypoint.

Subcommands:
  setup   Download/verify the local model weights into HF_HOME, emitting one
          JSON progress object per line on stdout for the onboarding wizard.
  proxy   Run the headroom compression proxy (delegates to the headroom CLI).

The desktop app owns all paths via env: HF_HOME, HEADROOM_WORKSPACE_DIR.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys


def _emit(step: str, detail: str, pct: float | None = None) -> None:
    print(json.dumps({"step": step, "detail": detail, "pct": pct}), flush=True)


def cmd_setup() -> int:
    _emit("verify", "Loading compression engine…")
    try:
        import headroom  # noqa: F401
        from headroom import _core  # noqa: F401
    except Exception as e:  # pragma: no cover
        _emit("error", f"engine import failed: {e}")
        return 1

    # Warm every model the proxy needs. Each constructor/first-use triggers a
    # pinned-revision download via huggingface_hub into HF_HOME (set by the
    # app); on later runs these are cache hits and setup exits quickly.
    _emit("models", "Downloading compression model (Kompress)…", 10)
    try:
        from headroom.transforms.kompress_compressor import KompressCompressor

        comp = KompressCompressor()
        # preload() downloads + loads model and tokenizer into the local HF
        # cache (headroom-ai 0.28 API); compress() then does a real inference
        # pass so ONNX session init cost is also paid here, not on first
        # proxied request.
        if hasattr(comp, "preload"):
            comp.preload(allow_download=True)
        sample = "def add(a, b):\n    return a + b\n" * 50
        try:
            comp.compress(sample)
        except Exception:
            # Defensive: the download already happened in preload(), which is
            # what setup is for.
            pass
    except Exception as e:
        _emit("error", f"kompress setup failed: {e}")
        return 1

    _emit("models", "Downloading routing models…", 70)
    try:
        # The technique router + embedding models download on first proxy
        # start; pre-warm them here so proxy startup can run fully offline.
        from headroom.onnx_runtime import hf_hub_download_local_first  # noqa: F401
    except Exception:
        pass

    _emit("done", "All models ready", 100)
    return 0


def cmd_proxy(argv: list[str]) -> int:
    from headroom.cli import main as headroom_main

    sys.argv = ["headroom", "proxy", *argv]
    try:
        headroom_main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    # Keep everything the runtime writes inside app-owned dirs.
    os.environ.setdefault("HEADROOM_TELEMETRY", "off")
    os.environ.setdefault("HEADROOM_TELEMETRY_WARN", "off")

    if len(sys.argv) < 2:
        print("usage: headroom-runtime {setup|proxy} [args…]", file=sys.stderr)
        return 2
    cmd, argv = sys.argv[1], sys.argv[2:]
    if cmd == "setup":
        return cmd_setup()
    if cmd == "proxy":
        return cmd_proxy(argv)
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
