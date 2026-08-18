"""Per-project cache-read accumulation for the context-reuse meter line."""

from __future__ import annotations

from headroom.proxy.savings_tracker import SavingsTracker


def test_project_rollup_accumulates_cache_read_tokens() -> None:
    tracker = SavingsTracker(stateless=True)

    tracker.record_request(
        model="gpt-test",
        input_tokens=1_000,
        tokens_saved=0,
        project="session-abc",
        cache_read_tokens=900,
        total_input_tokens=1_000,
    )
    tracker.record_request(
        model="gpt-test",
        input_tokens=2_000,
        tokens_saved=50,
        project="session-abc",
        cache_read_tokens=1_900,
        total_input_tokens=2_000,
    )

    projects = tracker.snapshot()["projects"]
    entry = projects["session-abc"]
    assert entry["requests"] == 2
    assert entry["cache_read_tokens"] == 2_800
    assert entry["tokens_saved"] == 50
