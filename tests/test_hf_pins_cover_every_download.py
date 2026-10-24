"""Every Hugging Face download in the package uses a pinned revision.

Halv's fork (see FORK.md) pins each Hub repository that shipping code fetches
to one commit SHA, so a changed or compromised repository cannot change what
Headroom loads. This file checks three things:

* every direct Hub call in ``headroom/`` passes ``revision=`` or sits in a
  helper that sets it from the pins, and no code builds a ``resolve/main`` URL;
* every repository that a default code path fetches has a pin;
* the helpers really pass the pinned SHA, and refuse a known repository that
  has no pin.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from headroom.onnx_runtime import _PINNED_REVISIONS, hf_from_pretrained_local_first

PACKAGE = Path(__file__).resolve().parents[1] / "headroom"

# Direct Hub entry points: a call to one of these opens a Hub download.
DIRECT_HUB_CALLS = frozenset(
    {
        "from_pretrained",
        "hf_hub_download",
        "hf_hub_url",
        "load_dataset",
        "SentenceTransformer",
        "snapshot_download",
        "TextEmbedding",
    }
)

# Helpers that put the pinned revision into ``**kwargs`` before the direct
# call. The behaviour tests below prove each one.
PINNING_HELPERS = frozenset(
    {
        ("evals/datasets.py", "load_hf_dataset"),
        ("relevance/embedding.py", "_load_text_embedding"),
    }
)

# Not on the Hub on 2026-10-24, so they cannot have a pin.
UNAVAILABLE_TOKENIZER_REPOS = frozenset({"mosaicml/mpt-7b", "mosaicml/mpt-30b"})

FULL_SHA = frozenset("0123456789abcdef")


def _is_sha(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= FULL_SHA


class _HubCallVisitor(ast.NodeVisitor):
    """Collect direct Hub calls with the outermost enclosing function."""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.found: list[tuple[str, str, int, str, list[str | None]]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = None
        if name in DIRECT_HUB_CALLS:
            keywords = [kw.arg for kw in node.keywords]
            outer = self.stack[0] if self.stack else "<module>"
            self.found.append((self.rel, outer, node.lineno, name, keywords))
        self.generic_visit(node)


def _hub_calls() -> list[tuple[str, str, int, str, list[str | None]]]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        visitor = _HubCallVisitor(path.relative_to(PACKAGE).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
        found.extend(visitor.found)
    return found


def test_every_direct_hub_call_is_pinned() -> None:
    calls = _hub_calls()
    assert calls, "the scan found no Hub calls; the scanner is broken"
    unpinned = [
        f"{rel}:{line} {name} in {func}"
        for rel, func, line, name, keywords in calls
        if "revision" not in keywords and (rel, func) not in PINNING_HELPERS
    ]
    assert unpinned == [], "Hub calls without revision=: " + ", ".join(unpinned)


def test_no_floating_resolve_main_url() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "huggingface.co/" in node.value and "/resolve/main" in node.value:
                    offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert offenders == []


def test_every_pin_is_a_full_commit_sha() -> None:
    from headroom.evals.datasets import _PINNED_DATASET_REVISIONS
    from headroom.relevance.embedding import _DEFAULT_MODEL_PINNED_REVISION

    for repo, sha in _PINNED_REVISIONS.items():
        assert _is_sha(sha), f"{repo}: {sha!r}"
    for path, sha in _PINNED_DATASET_REVISIONS.items():
        assert sha is None or _is_sha(sha), f"{path}: {sha!r}"
    assert _is_sha(_DEFAULT_MODEL_PINNED_REVISION)


def test_default_model_repos_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HEADROOM_SENTENCE_TRANSFORMER", "HEADROOM_SIGLIP", "HEADROOM_TECHNIQUE_ROUTER"):
        monkeypatch.delenv(var, raising=False)
    from headroom.image import onnx_router
    from headroom.memory.adapters.embedders import OnnxLocalEmbedder
    from headroom.models.config import MLModelConfig
    from headroom.transforms import kompress_compressor

    defaults = MLModelConfig()
    repos = {
        kompress_compressor.HF_MODEL_ID,
        kompress_compressor._MODERNBERT_TOKENIZER_REPO,
        onnx_router._TECHNIQUE_ROUTER_REPO,
        onnx_router._SIGLIP_ENCODER_REPO,
        OnnxLocalEmbedder.ONNX_REPO,
        defaults.sentence_transformer,
        defaults.siglip,
        defaults.technique_router,
    }
    missing = sorted(repo for repo in repos if repo not in _PINNED_REVISIONS)
    assert missing == []


def test_fastembed_default_model_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    from headroom.relevance.embedding import DEFAULT_MODEL_NAME, _pinned_revision

    assert _is_sha(_pinned_revision(DEFAULT_MODEL_NAME))


def test_every_shipped_tokenizer_repo_is_pinned() -> None:
    from headroom.tokenizers.huggingface import ALLOWED_TOKENIZER_REPOS

    missing = sorted(ALLOWED_TOKENIZER_REPOS - UNAVAILABLE_TOKENIZER_REPOS - set(_PINNED_REVISIONS))
    assert missing == []
    assert not UNAVAILABLE_TOKENIZER_REPOS & set(_PINNED_REVISIONS)


def test_every_eval_dataset_path_is_listed() -> None:
    from headroom.evals.datasets import _PINNED_DATASET_REVISIONS

    paths = set()
    for rel in ("evals/datasets.py", "evals/html_oss_benchmarks.py"):
        for node in ast.walk(ast.parse((PACKAGE / rel).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "load_hf_dataset"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                paths.add(node.args[0].value)
    assert paths, "the scan found no eval dataset loads"
    assert sorted(paths - set(_PINNED_DATASET_REVISIONS)) == []


# ---------------------------------------------------------------------------
# Behaviour of the pinning helpers
# ---------------------------------------------------------------------------


class _Loader:
    """A from_pretrained stand-in that records its calls."""

    def __init__(self, *, cached: bool) -> None:
        self.cached = cached
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, **kwargs: Any) -> object:
        self.calls.append({"name": name, **kwargs})
        if kwargs.get("local_files_only") and not self.cached:
            raise OSError("not cached")
        return object()


def test_from_pretrained_helper_pins_both_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    monkeypatch.delenv("HEADROOM_OFFLINE", raising=False)
    loader = _Loader(cached=False)
    repo = "google/siglip-base-patch16-224"

    hf_from_pretrained_local_first(loader, repo, purpose="test")

    assert [c["local_files_only"] for c in loader.calls] == [True, False]
    assert {c["revision"] for c in loader.calls} == {_PINNED_REVISIONS[repo]}


def test_from_pretrained_helper_pins_the_short_sentence_transformer_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    loader = _Loader(cached=True)

    hf_from_pretrained_local_first(loader, "all-MiniLM-L6-v2", purpose="test", device="cpu")

    assert loader.calls == [
        {
            "name": "all-MiniLM-L6-v2",
            "local_files_only": True,
            "device": "cpu",
            "revision": _PINNED_REVISIONS["sentence-transformers/all-MiniLM-L6-v2"],
        }
    ]


def test_from_pretrained_helper_keeps_explicit_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    loader = _Loader(cached=True)

    hf_from_pretrained_local_first(
        loader, "google/siglip-base-patch16-224", purpose="test", revision="a" * 40
    )

    assert loader.calls[0]["revision"] == "a" * 40


def test_from_pretrained_helper_leaves_local_dirs_and_unknown_repos_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    for name in (str(tmp_path), "acme/unknown-model"):
        loader = _Loader(cached=True)
        hf_from_pretrained_local_first(loader, name, purpose="test")
        assert "revision" not in loader.calls[0]


def test_from_pretrained_helper_floats_when_pinning_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEADROOM_HF_PIN", "off")
    loader = _Loader(cached=True)

    hf_from_pretrained_local_first(loader, "google/siglip-base-patch16-224", purpose="test")

    assert "revision" not in loader.calls[0]


@pytest.fixture
def fake_auto_tokenizer(monkeypatch: pytest.MonkeyPatch) -> _Loader:
    from headroom.tokenizers import huggingface as hf_mod

    loader = _Loader(cached=True)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=loader)),
    )
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    monkeypatch.delenv("HEADROOM_OFFLINE", raising=False)
    hf_mod._load_tokenizer.cache_clear()
    hf_mod._allowlist_index.cache_clear()
    yield loader
    hf_mod._load_tokenizer.cache_clear()
    hf_mod._allowlist_index.cache_clear()


def test_tokenizer_loads_at_the_pinned_revision(fake_auto_tokenizer: _Loader) -> None:
    from headroom.tokenizers.huggingface import _load_tokenizer

    repo = "meta-llama/Meta-Llama-3-8B"
    assert _load_tokenizer(repo) is not None
    assert fake_auto_tokenizer.calls[0]["revision"] == _PINNED_REVISIONS[repo]
    assert fake_auto_tokenizer.calls[0]["trust_remote_code"] is False


def test_tokenizer_without_a_pin_never_reaches_the_hub(fake_auto_tokenizer: _Loader) -> None:
    from headroom.tokenizers.huggingface import _load_tokenizer

    for repo in sorted(UNAVAILABLE_TOKENIZER_REPOS):
        assert _load_tokenizer(repo) is None
    assert fake_auto_tokenizer.calls == []


def test_operator_added_tokenizer_uses_the_default_ref(
    fake_auto_tokenizer: _Loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    from headroom.tokenizers.huggingface import _load_tokenizer

    monkeypatch.setenv("HEADROOM_HF_TOKENIZER_ALLOWLIST", "acme/private-tokenizer")
    assert _load_tokenizer("acme/private-tokenizer") is not None
    assert fake_auto_tokenizer.calls[0]["revision"] is None


@pytest.fixture
def fake_datasets(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def load_dataset(path: str, *args: Any, **kwargs: Any) -> list[Any]:
        calls.append({"path": path, **kwargs})
        return []

    monkeypatch.setitem(sys.modules, "datasets", types.SimpleNamespace(load_dataset=load_dataset))
    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    monkeypatch.delenv("HEADROOM_OFFLINE", raising=False)
    return calls


def test_eval_dataset_loads_at_the_pinned_revision(fake_datasets: list[dict[str, Any]]) -> None:
    from headroom.evals.datasets import _PINNED_DATASET_REVISIONS, load_hf_dataset

    load_hf_dataset("rajpurkar/squad_v2", split="validation")
    assert fake_datasets == [
        {
            "path": "rajpurkar/squad_v2",
            "split": "validation",
            "revision": _PINNED_DATASET_REVISIONS["rajpurkar/squad_v2"],
        }
    ]


def test_eval_dataset_without_a_pin_is_refused(fake_datasets: list[dict[str, Any]]) -> None:
    from headroom.evals.datasets import load_hf_dataset

    with pytest.raises(ValueError, match="no pinned revision"):
        load_hf_dataset("ToolBench/ToolBench", "G1", split="test")
    assert fake_datasets == []


def test_eval_dataset_floats_when_pinning_is_off(
    fake_datasets: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from headroom.evals.datasets import load_hf_dataset

    monkeypatch.setenv("HEADROOM_HF_PIN", "off")
    load_hf_dataset("rajpurkar/squad_v2", split="validation")
    assert "revision" not in fake_datasets[0]


def test_bfcl_download_uses_the_pinned_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    from headroom.evals.datasets import _PINNED_DATASET_REVISIONS, load_bfcl

    monkeypatch.delenv("HEADROOM_HF_PIN", raising=False)
    monkeypatch.delenv("HEADROOM_OFFLINE", raising=False)
    urls: list[str] = []

    def fake_urlopen(url: str, *args: Any, **kwargs: Any) -> Any:
        urls.append(url)
        raise OSError("no network in tests")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError):
        load_bfcl(n=1, category="simple")
    sha = _PINNED_DATASET_REVISIONS["gorilla-llm/Berkeley-Function-Calling-Leaderboard"]
    assert urls and all(f"/resolve/{sha}/" in url for url in urls)
