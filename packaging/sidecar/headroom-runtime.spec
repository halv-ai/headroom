# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the headroom-runtime desktop sidecar (onedir).

Build:  .venv/bin/pyinstaller --clean --noconfirm headroom-runtime.spec

Philosophy: big but correct beats small but broken. The runtime is shipped
inside a desktop app bundle; a few extra MB of collected submodules is
cheaper than a support ticket for a missing lazy import.
"""

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

datas = []
binaries = []
hiddenimports = []


def _merge(pkgs, *, data_only=()):
    """collect_all() the given packages, tolerating absent optionals."""
    global datas, binaries, hiddenimports
    for pkg in pkgs:
        try:
            d, b, h = collect_all(pkg)
        except Exception:
            continue
        datas += d
        binaries += b
        hiddenimports += h
    for pkg in data_only:
        try:
            datas += collect_data_files(pkg)
        except Exception:
            pass


# ── Full collection (code + data + compiled extensions) ─────────────────
# headroom: the whole package, incl. headroom._core (Rust abi3 extension),
#   dashboard HTML templates, tools.json, tokenizers data.
# magika: content-type detection — ships its ONNX model + config JSON under
#   magika/models/ and magika/config/; missing them breaks ContentRouter.
# onnxruntime: libonnxruntime dylib lives inside the package.
# sqlite_vec: loadable sqlite extension (vec0) shipped as package data.
# tiktoken / tiktoken_ext: BPE registries discovered via namespace-package
#   scan at runtime — static analysis cannot see tiktoken_ext.openai_public.
_merge(
    [
        "headroom",
        "magika",
        "onnxruntime",
        "sqlite_vec",
        "tiktoken",
        "tiktoken_ext",
    ]
)

# ── Submodule collection (lazy/dynamic imports) ──────────────────────────
for pkg in (
    "uvicorn",           # loops/protocols selected by string at runtime
    "fastapi",
    "starlette",
    "websockets",        # uvicorn ws protocol picked dynamically
    "zstandard",
    "tokenizers",
    "huggingface_hub",
    "transformers",      # imported lazily by kompress_compressor
    "safetensors",
    "anyio",
    "sse_starlette",
    "hf_xet",            # optional huggingface_hub accelerated backend
):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# Data files for packages that read non-.py resources at runtime.
_merge(
    [],
    data_only=(
        "transformers",
        "huggingface_hub",
        "litellm",       # model_prices_and_context_window.json etc.
        "certifi",
    ),
)

# ── Distribution metadata (importlib.metadata.version() at runtime) ─────
# headroom/_version.py resolves version("headroom-ai"); transformers and
# huggingface_hub run dependency version checks via importlib.metadata.
for dist in (
    "headroom-ai",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "safetensors",
    "tiktoken",
    "regex",
    "requests",
    "packaging",
    "filelock",
    "numpy",
    "pyyaml",
    "tqdm",
    "onnxruntime",
    "litellm",
    "fastapi",
    "uvicorn",
    "openai",
    "httpx",
    "aiohttp",
    "pydantic",
    "hf-xet",
):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

excludes = [
    # Heavy optional ML stacks not installed in the sidecar venv — keep the
    # analyzer from chasing them through transformers' lazy import graph.
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "jax",
    "flax",
    # GUI / plotting / notebook stacks.
    "matplotlib",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "jupyter",
    "notebook",
    # Test frameworks.
    "pytest",
    "_pytest",
    "unittest.mock",
]

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="headroom-runtime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="headroom-runtime",
)
