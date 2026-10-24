# About this fork

This repository is Halv's fork of Headroom. Halv uses it to build the Headroom
proxy runtime that the Halv desktop app ships.

- Upstream: <https://github.com/headroomlabs-ai/headroom>
- Base: upstream release `v0.41.0`, commit `76133cff59b71284399bf760c5578dc373c97231`
- This fork is not affiliated with or endorsed by Headroom Labs. "Headroom" is
  the name of the upstream project. This file does not claim a trademark right.
- License: Apache License 2.0, the same as upstream. See [LICENSE](LICENSE) and
  [NOTICE](NOTICE).

The history keeps every earlier fork commit. Older Halv releases pin some of
those commits, so they stay reachable from `main`.

## What this fork changes

The groups below list each file that differs from `v0.41.0`. To see the full
list yourself, run `git diff --stat 76133cf..main`.

### 1. OpenAI Responses and Codex compression

The fork adds an opt-in mode that compresses older OpenAI Responses message
items with a deterministic extractive transform. The newest six message items
stay verbatim. The mode is off unless you set
`HEADROOM_COMPRESS_RESPONSES_MESSAGES=1`, pass `--compress-responses-messages`,
or send the `x-headroom-compress-messages: true` header. The proxy removes that
header and `x-headroom-protect-recent` before it forwards a request.

The fork also does these things on the Responses and Codex WebSocket paths:

- It compresses every Codex WebSocket request frame and keeps tool definitions
  on the frame.
- It keeps MCP and code-index tool output verbatim. The patterns are in
  `HEADROOM_COMPRESS_TOOL_OUTPUT_EXCLUDE` and
  `HEADROOM_COMPRESS_CONTENT_EXCLUDE`.
- It uses a lossless mode for incremental clients (`HEADROOM_INCREMENTAL_LOSSLESS`,
  `HEADROOM_INCREMENTAL_CCR`).
- It counts Responses input tokens on the native Responses shape, tools
  included.

Files:

- `headroom/proxy/handlers/openai.py`
- `headroom/transforms/compression_units.py`
- `headroom/transforms/compression_batches.py`
- `headroom/proxy/models.py`
- `headroom/proxy/server.py`
- `headroom/cli/proxy.py`
- `headroom/settings_store.py`
- `.env.example`
- `README.md` (the section "OpenAI Responses message compression")

### 2. Upstream TLS retry

The proxy retries a request once after an `ssl.SSLError` on the buffered, the
streaming and the mid-stream paths, and it logs the error.

Files:

- `headroom/proxy/helpers.py`
- `headroom/proxy/handlers/streaming.py`
- `headroom/proxy/server.py`

### 3. Savings accounting

- The per-project savings rollup records `cache_read_tokens`.
- The `agent_usage` savings row counts conversation savings once per
  conversation, so it agrees with `tokens.saved` and the per-project figure.

Files:

- `headroom/proxy/savings_tracker.py`
- `headroom/proxy/outcome.py`
- `headroom/proxy/handlers/openai.py`

### 4. Frozen runtime start on macOS

Upstream re-executes the process on macOS to set malloc options. In a
PyInstaller binary, that re-execution starts the wrong program and exits with
code 2. The fork skips the re-execution in a frozen binary.

Files:

- `headroom/cli/proxy.py`

### 5. Upload beacon off by default

Upstream sets `BEACON_DEFAULT_ON = True` in `headroom/telemetry/beacon.py`.
With that value, every install uploads anonymous session summaries to
`headroom-beacon.headroom-beacon.workers.dev` unless it opts out.

This fork sets `BEACON_DEFAULT_ON = False`:

- The beacon uploads nothing until you set `HEADROOM_BEACON=on` (or `true`,
  `1`, `yes`, `enable`, `enabled`).
- An unset, empty or unknown value keeps the beacon off.
- `DO_NOT_TRACK=1` and `HEADROOM_OFFLINE=1` keep it off even after an opt-in.
- `HEADROOM_TELEMETRY` (local stats only) does not change.

`tests/test_beacon_default_off.py` checks that no aggregator, no beacon thread
and no POST exist without the opt-in.

Files:

- `headroom/telemetry/beacon.py` (the default)
- Comments and messages: `headroom/telemetry/session.py`, `headroom/proxy/server.py`,
  `headroom/proxy/prometheus_metrics.py`, `headroom/proxy/outcome.py`,
  `headroom/cli/proxy.py`, `headroom/cli/telemetry.py`, `headroom/ccr/mcp_server.py`,
  `headroom/update_check.py`
- Documents: `README.md`, `SECURITY.md`, `llms.txt`,
  `docs/content/docs/security-model.mdx`, `docs/content/docs/limitations.mdx`,
  `docs/content/docs/proxy.mdx`, `docs/metrics-technical-guide.md`

The update check (`HEADROOM_UPDATE_CHECK`) keeps the upstream default. Halv
turns it off when it starts the proxy.

### 6. Hugging Face downloads pinned to commits

Upstream pins some model downloads to a commit SHA. The fork pins every
Hugging Face download in `headroom/` that used the floating `main` ref:

- `hf_from_pretrained_local_first` applies the pin when a caller does not pass
  `revision=`.
- The token-counting tokenizer loader uses a pinned revision for each
  allowlisted repository.
- The eval dataset loaders and the BFCL download use pinned commits.

`HEADROOM_HF_PIN=off` turns pinning off, as upstream. A model name that you
choose through an environment variable, and a tokenizer repository that you add
through `HEADROOM_HF_TOKENIZER_ALLOWLIST`, have no pin.
`tests/test_hf_pins_cover_every_download.py` scans `headroom/` for Hub calls
without `revision=`.

Files:

- `headroom/onnx_runtime.py` (the pin table)
- `headroom/tokenizers/huggingface.py`
- `headroom/evals/datasets.py`
- `scripts/ci/verify_hf_model_cache.py`
- `benchmarks/comprehensive_eval.py`, `benchmarks/i18n_compression_eval.py`

See [Hugging Face pins](#hugging-face-pins) for the table.

### 7. Removed fork-only files

The fork removed files that it added before and no longer uses: `cloud/` (an
unused Supabase skeleton), `packaging/` (an old sidecar build), and the
workflows `cloud-deploy.yml`, `sidecar-release.yml` and `upstream-sync.yml`.
Upstream never had these files, so they are not in the diff against `v0.41.0`.

### 8. Notices and tests

- `FORK.md` (this file), `NOTICE` (the modification notice) and `README.md`
  (the banner at the top).
- Each Python source file that the fork changes has the comment
  `# Modified by Halv. See FORK.md.` near the top. This follows section 4(b)
  of the Apache License 2.0.
- Tests:
  `tests/conftest.py`, `tests/test_beacon_default_off.py`,
  `tests/test_cli_proxy_env.py`, `tests/test_cli_proxy_malloc_reexec_guard.py`,
  `tests/test_compression_batches.py`, `tests/test_compression_units.py`,
  `tests/test_dashboard_agent_usage.py`, `tests/test_evals_datasets.py`,
  `tests/test_h2_stream_reset_retry.py`, `tests/test_hf_pins_cover_every_download.py`,
  `tests/test_openai_codex_routing.py`, `tests/test_openai_codex_ws_lifecycle.py`,
  `tests/test_openai_responses_compression_units.py`,
  `tests/test_openai_responses_context_compaction.py`,
  `tests/test_openai_responses_read_protection.py`,
  `tests/test_openai_responses_t3_replay_regression.py`,
  `tests/test_openai_responses_tool_output_exemption.py`,
  `tests/test_project_cache_read_rollup.py`, `tests/test_proxy_openai.py`,
  `tests/test_proxy_passthrough_transient_retry.py`,
  `tests/test_proxy_streaming_request_logger.py`, `tests/test_telemetry_warning.py`

## Hugging Face pins

Each revision is a full commit SHA. For the repositories that upstream already
pinned, the fork keeps the upstream pin. For the new pins, the SHA is the
`main` commit of the repository on 2026-10-24. Sizes are the files that the
code fetches at that revision.

### Models

| Hugging Face repository | Revision | Files | Size | Feature that downloads it | How to turn it off |
|---|---|---|---|---|---|
| `chopratejas/kompress-v2-base` | `b1563631b35bfdcee37587ad530147497d820d4c` (upstream pin) | `onnx/kompress-int8-wo.onnx` (fallback `onnx/kompress-fp32.onnx`, 600.6 MB; PyTorch path `merged.pt` or `model.safetensors`, about 300 MB) | 274.0 MB | Kompress text compression. The proxy prefetches the files at startup and loads the model on first use. | `HEADROOM_DISABLE_KOMPRESS=1`, or leave `kompress` out of `HEADROOM_COMPRESSORS` |
| `answerdotai/ModernBERT-base` | `8949b909ec900327062f0ebf497f51aef5e6f0c8` (upstream pin) | `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`, `config.json` (PyTorch path also `model.safetensors`, 598.6 MB) | 2.2 MB | Kompress tokenizer | Same as Kompress |
| `chopratejas/technique-router-onnx` | `27b0b4bfa510a1cff66d888072c0b807082721a8` (upstream pin) | `model_quantized.onnx`, `tokenizer.json`, `config.json` | 34.7 MB | Image compression (on by default), on the first request that has an image | Leave `image` out of `HEADROOM_COMPRESSORS` |
| `chopratejas/siglip-image-encoder-onnx` | `d0a9fbd66d4bd8c761bff592d44831f7c2ae184e` (upstream pin) | `image_encoder_int8.onnx` | 99.1 MB | Image compression router (SigLIP encoder) | Same as image compression |
| `Qdrant/all-MiniLM-L6-v2-onnx` | `5f1b8cd78bc4fb444dd171e59b18f3a3af89a079` (upstream pin) | `model.onnx`, `tokenizer.json` | 91.1 MB | Memory with the ONNX embedder | Memory is off by default (`--memory` or `--learn` turns it on) |
| `qdrant/bge-small-en-v1.5-onnx-q` (fastembed name `BAAI/bge-small-en-v1.5`) | `52398278842ec682c6f32300af41344b1c0b0bb2` (upstream pin) | `model_optimized.onnx` and tokenizer files | 67.2 MB | Embedding relevance scorer. Needs the `fastembed` package (extra `relevance`). | Do not install `fastembed` |
| `sentence-transformers/all-MiniLM-L6-v2` (default name `all-MiniLM-L6-v2`) | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (new) | `model.safetensors`, config and tokenizer files | 91.6 MB | Memory with the local embedder, dynamic content detection, eval metrics. Needs `sentence-transformers` and PyTorch. | Memory is off by default. `HEADROOM_SENTENCE_TRANSFORMER` selects another model, which has no pin. |
| `google/siglip-base-patch16-224` | `7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed` (new) | `model.safetensors`, `spiece.model`, config and tokenizer files | 815.9 MB | PyTorch fallback of the image router, when the ONNX router fails. Needs PyTorch. | Same as image compression. `HEADROOM_SIGLIP` selects another model, which has no pin. |
| `chopratejas/technique-router` | `639f08ab1fac0a0eb888bbeb80e752dbf8a780c1` (new) | `model.safetensors`, `vocab.txt`, config and tokenizer files | 134.4 MB | PyTorch fallback of the image router. Needs PyTorch. | Same as image compression. `HEADROOM_TECHNIQUE_ROUTER` selects another model, which has no pin. |

### Token-counting tokenizers

The proxy counts tokens with a Hugging Face tokenizer for model names that
start with `llama`, `meta-llama`, `codellama`, `phi-`, `qwen`, `deepseek`,
`yi-`, `falcon`, `mpt-`, `starcoder` or `codegen`. It fetches only the
tokenizer files (`tokenizer.json`, `tokenizer_config.json`,
`special_tokens_map.json`, `config.json`, and a vocabulary file where the
repository has one). The size is from 2.3 KB to 21.8 MB for each repository.

The network load stops after `HEADROOM_HF_TOKENIZER_LOAD_TIMEOUT_SECS` (default
10). Set it to `0` to turn off tokenizer downloads; the proxy then estimates
the count. Gated repositories (Meta Llama, Gemma, Mistral Large, StarCoder,
Falcon 180B) need `HF_TOKEN`. Without it, the proxy estimates the count.

| Hugging Face repository | Revision |
|---|---|
| `01-ai/Yi-1.5-6B` | `157a3d77ab5a8a8f079510eaa25169b547481f06` |
| `01-ai/Yi-34B` | `e1fa7c83283f2e1d5a12cb4adea37c3f829e28f8` |
| `01-ai/Yi-6B` | `80080be87ec5a0103f643195f2d9003b8068941b` |
| `bigcode/starcoder` | `827cd7721231872c152e3faef5cae1b22964ed58` |
| `bigcode/starcoder2-15b` | `46d44742909c03ac8cee08eb03fdebce02e193ec` |
| `bigcode/starcoder2-3b` | `733247c55e3f73af49ce8e9c7949bf14af205928` |
| `bigcode/starcoder2-7b` | `bb9afde76d7945da5745592525db122d4d729eb1` |
| `codellama/CodeLlama-13b-hf` | `8da65ff4ee20f74ecd107ca9d54f9f121b279860` |
| `codellama/CodeLlama-34b-hf` | `6008b9656730b71c7d19a15370c7ff6d2902f4ef` |
| `codellama/CodeLlama-7b-hf` | `6c284d1468fe6c413cf56183e69b194dcfa27fe6` |
| `deepseek-ai/deepseek-coder-6.7b-base` | `ce2207a8bfef3ee92bd7dd4cc31c52cfa0046912` |
| `deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct` | `e434a23f91ba5b4923cf6c9d9a238eb4a08e3a11` |
| `deepseek-ai/deepseek-llm-67b-base` | `c3f813a1121c95488a20132d3a4da89f4a46452f` |
| `deepseek-ai/deepseek-llm-7b-base` | `7683fea62db869066ddaff6a41d032262c490d4f` |
| `deepseek-ai/DeepSeek-R1` | `56d4cbbb4d29f4355bab4b9a39ccb717a14ad5ad` |
| `deepseek-ai/DeepSeek-R1-0528` | `4236a6af538feda4548eca9ab308586007567f52` |
| `deepseek-ai/DeepSeek-R1-Distill-Llama-8B` | `6a6f4aa4197940add57724a7707d069478df56b1` |
| `deepseek-ai/DeepSeek-R1-Distill-Qwen-7B` | `916b56a44061fd5cd7d6a8fb632557ed4f724f60` |
| `deepseek-ai/DeepSeek-V2` | `4461458f186c35188585855f28f77af5661ad489` |
| `deepseek-ai/DeepSeek-V2-Lite` | `604d5664dddd88a0433dbae533b7fe9472482de0` |
| `deepseek-ai/DeepSeek-V3` | `e815299b0bcbac849fa540c768ef21845365c9eb` |
| `deepseek-ai/DeepSeek-V3-0324` | `e9b33add76883f293d6bf61f6bd89b497e80e335` |
| `deepseek-ai/DeepSeek-V3.2` | `a7e62ac04ecb2c0a54d736dc46601c5606cf10a6` |
| `deepseek-ai/DeepSeek-V4-Pro` | `b5968e9190ef611bbf34a7229255be88a0e937c1` |
| `deepseek-ai/DeepSeek-V4.1-Flash` | `314113f8a7dadb388215396ba5fbee2c610a04d6` |
| `google/gemma-2-27b` | `938270f5272feb02779b55c2bb2fffdd0f53ff0c` |
| `google/gemma-2-9b` | `33c193028431c2fde6c6e51f29e6f17b60cbfac6` |
| `google/gemma-2b` | `9cf48e52b224239de00d483ec8eb84fb8d0f3a3a` |
| `google/gemma-7b` | `ff6768d9368919a1f025a54f9f5aa0ee591730bb` |
| `meta-llama/Llama-2-13b-hf` | `5c31dfb671ce7cfe2d7bb7c04375e44c55e815b1` |
| `meta-llama/Llama-2-70b-hf` | `3aba440b59558f995867ba6e1f58f21d0336b5bb` |
| `meta-llama/Llama-2-7b-hf` | `01c7f73d771dfac7d292323805ebc428287df4f9` |
| `meta-llama/Llama-3.1-405B` | `b906e4dc842aa489c962f9db26554dcfdde901fe` |
| `meta-llama/Llama-3.1-70B` | `349b2ddb53ce8f2849a6c168a81980ab25258dac` |
| `meta-llama/Llama-3.1-8B` | `d04e592bb4f6aa9cfee91e2e20afa771667e1d4b` |
| `meta-llama/Llama-3.2-1B` | `4e20de362430cd3b72f300e6b0f18e50e7166e08` |
| `meta-llama/Llama-3.2-3B` | `13afe5124825b4f3751f836b40dafda64c1ed062` |
| `meta-llama/Llama-3.3-70B-Instruct` | `6f6073b423013f6a7d4d9f39144961bfbfbc386b` |
| `meta-llama/Meta-Llama-3-70B` | `c82494877ce7f6d7d317c56ec081328e382c72fe` |
| `meta-llama/Meta-Llama-3-8B` | `8cde5ca8380496c9a6cc7ef3a8b46a0372a1d920` |
| `microsoft/phi-2` | `810d367871c1d460086d9f82db8696f2e0a0fcd0` |
| `microsoft/Phi-3-medium-4k-instruct` | `48d87cd5e0523b430d77e93becd3655cd6897230` |
| `microsoft/Phi-3-mini-4k-instruct` | `f39ac1d28e925b323eae81227eaba4464caced4e` |
| `microsoft/Phi-3-small-8k-instruct` | `188b876e997ee4055df01afe46274c14c400fe40` |
| `mistralai/Mistral-7B-Instruct-v0.2` | `63a8b081895390a26e140280378bc85ec8bce07a` |
| `mistralai/Mistral-7B-Instruct-v0.3` | `c170c708c41dac9275d15a8fff4eca08d52bab71` |
| `mistralai/Mistral-7B-v0.1` | `27d67f1b5f57dc0953326b2601d68371d40ea8da` |
| `mistralai/Mistral-Large-Instruct-2407` | `a286006d554cb37a61d13c7ae61bc90cc1d372fc` |
| `mistralai/Mistral-Nemo-Base-2407` | `a4477a2f977929a969745b69bbd62e03043551a5` |
| `mistralai/Mistral-Small-Instruct-2409` | `4600506f6b13c7ef89e61a54263f4c9bf483de30` |
| `mistralai/Mixtral-8x22B-v0.1` | `e1cd34ff1747406fb2277635ed25242803009bc2` |
| `mistralai/Mixtral-8x7B-v0.1` | `fc7ac94680e38d7348cfa806e51218e6273104b0` |
| `Qwen/Qwen-14B` | `c4051215126d906ac22bb67fe5edb39a921cd831` |
| `Qwen/Qwen-72B` | `b8e18ac61df64d35308695769ff46b976b6a00f4` |
| `Qwen/Qwen-7B` | `ef3c5c9c57b252f3149c1408daf4d649ec8b6c85` |
| `Qwen/Qwen2-72B` | `5f149cae83c11c408a953dec64b12e7e662e8cb9` |
| `Qwen/Qwen2-7B` | `453ed1575b739b5b03ce3758b23befdb0967f40e` |
| `Qwen/Qwen2.5-72B` | `efba10c8e54e91e0d9570ab5f7b51a958474d4cb` |
| `Qwen/Qwen2.5-7B` | `d149729398750b98c0af14eb82c78cfe92750796` |
| `tiiuae/falcon-180B` | `d2ea5531862d4fe907280234990e6380d2befd97` |
| `tiiuae/falcon-40b` | `05ab2ee8d6b593bdbab17d728de5c028a7a94d83` |
| `tiiuae/falcon-7b` | `ec89142b67d748a1865ea4451372db8313ada0d8` |
| `mosaicml/mpt-7b`, `mosaicml/mpt-30b` | none: not on the Hub on 2026-10-24. The loader refuses them and estimates the count. |

### Eval datasets

Only the `headroom evals` commands and the benchmark scripts load these. The
proxy never does. Each load fetches one configuration and split, which is a
part of the repository size below.

| Hugging Face dataset | Revision | Repository size |
|---|---|---|
| `hotpotqa/hotpot_qa` | `1908d6afbbead072334abe2965f91bd2709910ab` | 746.6 MB |
| `google-research-datasets/natural_questions` | `e8103d566bef4154c2c12b17c6095ec5275840cc` | 58.2 GB |
| `trivia_qa` (now `mandarjoshi/trivia_qa`) | `0f7faf33a3908546c6fd5b73a660e0f8ff173c2f` | 34.8 GB |
| `microsoft/ms_marco` | `a47ee7aae8d7d466ba15f9f0bfac3b3681087b3a` | 2.3 GB |
| `rajpurkar/squad_v2` | `3ffb306f725f7d2ce8394bc1873b24868140c412` | 17.7 MB |
| `THUDM/LongBench` (now `zai-org/LongBench`) | `5e628be450b7e67fb7ae6e201bd6d8f7056f7672` | 114.0 MB |
| `alexandrainst/multi-wiki-qa` | `9bdb3350487e399bd1cd873e1395290d636db5ce` | 558.0 MB |
| `deepmind/narrativeqa` | `2e643e7363944af1c33a652d1c87320d0871c4e4` | 3.4 GB |
| `code_search_net` (now `code-search-net/code_search_net`) | `bd0cf261e357a3eb5c8fba490d23ec1a1cd59555` | 3.9 GB |
| `openai_humaneval` (now `openai/openai_humaneval`) | `7dce6050a7d6d172f3cc5c32aa97f52fa1a2e544` | 0.1 MB |
| `allenai/scrapinghub-article-extraction-benchmark` | `f74b11e8b2e38f3828c3052622d108a22de53319` | 10.4 MB |
| `gorilla-llm/Berkeley-Function-Calling-Leaderboard` | `61fc0608cfd831fcfbbaa676ebdfef0ed963eeda` | 11.7 MB (two JSON files per category) |
| `ToolBench/ToolBench` | none: not on the Hub on 2026-10-24. The loader refuses it while pinning is on. | |

### Rust crates

The Rust crate `headroom-core` has three Hub download functions that use the
`main` ref: `HfTokenizer::from_pretrained`, `Kompress::from_pretrained` and
`EmbeddingScorer::try_new`. In this tree only tests and documentation examples
call them. The Python extension `headroom._core` and the proxy runtime do not.
The live-zone Kompress warmup uses `Kompress::from_cache`, which reads only the
local cache. The fork does not change these functions.

## Publishing and continuous integration

This repository does not run upstream's release or publishing workflows.
GitHub Actions is turned off for this repository. The upstream workflows stay
in `.github/workflows/` without change, so that future upstream merges stay
simple. This fork does not publish packages to PyPI, container images or
release binaries.

## How Halv pins and builds the runtime

Halv pins one exact commit of this repository in its lock file, together with
the SHA-256 of `uv.lock` and the versions of Python, uv and PyInstaller. The
Halv build installs the locked dependencies and freezes the proxy with
PyInstaller. For the full procedure and the commands to reproduce a runtime,
see [REPRODUCING.md](https://github.com/halv-ai/halv/blob/main/REPRODUCING.md).

Halv starts the proxy with `HEADROOM_BEACON=off`, `DO_NOT_TRACK=1` and
`HEADROOM_UPDATE_CHECK=off`, in addition to the fork defaults above.

## License

This fork is licensed under the Apache License 2.0, the same as upstream
Headroom. The upstream copyright and license notices stay in place. The
modifications are by Pedro Villaca (Halv), 2026.
