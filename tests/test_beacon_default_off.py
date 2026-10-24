"""The upload beacon stays off unless the operator opts in.

Upstream Headroom ships ``BEACON_DEFAULT_ON = True``. This fork (see FORK.md)
ships False, so nothing goes to the beacon endpoint without an explicit
``HEADROOM_BEACON=on``. These tests drive the real beacon entry points and
assert that, without that opt-in, no aggregator is built, no beacon thread
starts, and no POST is attempted.
"""

from __future__ import annotations

import threading
import urllib.request
from types import SimpleNamespace

import pytest

from headroom.telemetry import beacon, session


def _clear_beacon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HEADROOM_BEACON", "DO_NOT_TRACK", "HEADROOM_OFFLINE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def network_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record every way a beacon event could leave the process.

    The live aggregator is reset to None for the test, so a call that builds
    one is visible. Every sink is replaced, so even a regression cannot reach
    the real endpoint from the test run.
    """
    calls: dict[str, list] = {"urlopen": [], "send": [], "post": [], "threads": []}
    monkeypatch.setattr(session, "_aggregator", None)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls["urlopen"].append((a, k)))
    monkeypatch.setattr(session, "_send", lambda *a, **k: calls["send"].append((a, k)))
    monkeypatch.setattr(session, "post_session_event", lambda p: calls["post"].append(p))

    real_start = threading.Thread.start

    def spy_start(self: threading.Thread) -> None:
        if self.name == "headroom-beacon":
            calls["threads"].append(self.name)
            return
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy_start)
    return calls


def _outcome() -> SimpleNamespace:
    return SimpleNamespace(
        provider="anthropic",
        model="claude-test",
        status=200,
        original_tokens=1000,
        attempted_input_tokens=1000,
        optimized_tokens=400,
        tokens_saved=600,
    )


def test_default_constant_is_off() -> None:
    assert beacon.BEACON_DEFAULT_ON is False


@pytest.mark.parametrize("value", [None, "", "   ", "maybe", "auto", "default", "onn"])
def test_beacon_is_off_without_an_explicit_on_value(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    _clear_beacon_env(monkeypatch)
    if value is not None:
        monkeypatch.setenv("HEADROOM_BEACON", value)
    assert beacon.is_beacon_enabled() is False


@pytest.mark.parametrize("value", ["on", "ON", "true", "1", "yes", "enable", "enabled"])
def test_explicit_on_value_opts_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _clear_beacon_env(monkeypatch)
    monkeypatch.setenv("HEADROOM_BEACON", value)
    assert beacon.is_beacon_enabled() is True


@pytest.mark.parametrize("guard", [("DO_NOT_TRACK", "1"), ("HEADROOM_OFFLINE", "1")])
def test_do_not_track_and_offline_win_over_an_opt_in(
    monkeypatch: pytest.MonkeyPatch, guard: tuple[str, str]
) -> None:
    _clear_beacon_env(monkeypatch)
    monkeypatch.setenv("HEADROOM_BEACON", "on")
    monkeypatch.setenv(*guard)
    assert beacon.is_beacon_enabled() is False


def test_no_upload_without_opt_in(
    monkeypatch: pytest.MonkeyPatch, network_spy: dict[str, list]
) -> None:
    _clear_beacon_env(monkeypatch)

    for _ in range(3):
        session.record_outcome(_outcome())
    session.record_mcp_compression(original_tokens=1000, compressed_tokens=400)
    # The atexit flush only reports a session that already exists.
    session._flush_at_exit()

    assert session._aggregator is None
    assert network_spy == {"urlopen": [], "send": [], "post": [], "threads": []}


def test_opt_in_reaches_the_aggregator(
    monkeypatch: pytest.MonkeyPatch, network_spy: dict[str, list]
) -> None:
    """Positive control: the spy can see a beacon event when one is made."""
    _clear_beacon_env(monkeypatch)
    monkeypatch.setenv("HEADROOM_BEACON", "on")
    recorded: list[object] = []
    monkeypatch.setattr(
        session,
        "get_session_aggregator",
        lambda: SimpleNamespace(record=lambda outcome, **_: recorded.append(outcome)),
    )

    session.record_outcome(_outcome())
    session.record_mcp_compression(original_tokens=1000, compressed_tokens=400)

    assert len(recorded) == 2
    assert network_spy["urlopen"] == []
