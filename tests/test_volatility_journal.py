"""The in-memory volatility ring and the endpoint that exposes it.

WHY A RING AND NOT A TABLE — the property worth pinning is that this buffer is
BOUNDED. The volatility node runs on every analysis cycle and every monitoring
tick per open position; an unbounded list here would be the same unbounded growth
the operator asked to keep out of the database, just moved into RAM.
"""

from __future__ import annotations

import pytest

from backend.services import volatility_journal


@pytest.fixture(autouse=True)
def _clean_journal():
    volatility_journal.reset()
    yield
    volatility_journal.reset()


def _record(run_id: str, symbol: str = "BTC/USDT", ts: float = 0.0, **reading):
    volatility_journal.record(
        run_id=run_id,
        symbol=symbol,
        timeframe="15m",
        ts=ts,
        reading={"regime": "NORMAL", "trading_allowed": True, **reading},
    )


def test_a_reading_comes_back_with_its_identity_and_measurements():
    _record("run-1", ts=123.0, atr_percent=0.4)
    (entry,) = volatility_journal.recent()

    # The id is what the frontend's capped file stores against, so a reading can
    # be matched back to the run that produced it.
    assert entry["id"] == "run-1:BTC/USDT"
    assert entry["runId"] == "run-1"
    assert entry["ts"] == 123.0
    assert entry["atr_percent"] == 0.4


def test_the_buffer_is_bounded():
    for i in range(volatility_journal.MAX_ENTRIES + 50):
        _record(f"run-{i}", ts=float(i))

    assert volatility_journal.size() == volatility_journal.MAX_ENTRIES
    # The OLDEST are the ones dropped — a ring that evicted the newest would hold
    # a permanently stale picture of a market that had moved on.
    ids = {e["runId"] for e in volatility_journal.recent(limit=volatility_journal.MAX_ENTRIES)}
    assert f"run-{volatility_journal.MAX_ENTRIES + 49}" in ids
    assert "run-0" not in ids


def test_readings_come_back_newest_first():
    _record("old", ts=1.0)
    _record("new", ts=2.0)
    assert [e["runId"] for e in volatility_journal.recent()] == ["new", "old"]


def test_two_symbols_in_one_run_are_two_readings():
    # Keying on run_id alone would silently discard one of them, and the two
    # instruments genuinely have different volatility.
    _record("run-1", symbol="BTC/USDT")
    _record("run-1", symbol="ETH/USDT")
    assert volatility_journal.size() == 2
    assert len(volatility_journal.recent(symbol="ETH/USDT")) == 1


def test_limit_bounds_the_response_not_the_buffer():
    for i in range(10):
        _record(f"run-{i}", ts=float(i))
    assert len(volatility_journal.recent(limit=3)) == 3
    assert volatility_journal.size() == 10


def test_the_node_records_into_the_journal():
    """The wiring itself — a journal nothing writes to is an empty page.

    Runs the real node over a hand-built series rather than mocking `record`, so
    this fails if the call is removed OR if the node stops reaching it.
    """
    from backend.graphs.nodes.market import analyse_volatility_node
    from backend.graphs.state import MarketSnapshot

    candles = [{"high": 100.5, "low": 99.5, "close": 100.0, "open": 100.0, "volume": 1.0} for _ in range(200)]
    snapshot = MarketSnapshot(symbol="BTC/USDT", price=100.0, candles={"15m": candles})

    out = analyse_volatility_node({"symbol": "BTC/USDT", "market_data": snapshot, "run_id": "run-9"})

    assert out["volatility"].regime is not None
    (entry,) = volatility_journal.recent()
    assert entry["id"] == "run-9:BTC/USDT"
    assert entry["regime"] == out["volatility"].regime


def test_a_journal_failure_cannot_fail_a_graph_run(monkeypatch):
    """Telemetry must never be able to stop the agent measuring volatility."""
    from backend.graphs.nodes import market as market_nodes
    from backend.graphs.state import MarketSnapshot

    def boom(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(market_nodes.volatility_journal, "record", boom)

    candles = [{"high": 100.5, "low": 99.5, "close": 100.0, "open": 100.0, "volume": 1.0} for _ in range(200)]
    out = market_nodes.analyse_volatility_node(
        {"symbol": "BTC/USDT", "market_data": MarketSnapshot(symbol="BTC/USDT", price=100.0, candles={"15m": candles}), "run_id": "r"}
    )
    assert out["volatility"].regime is not None
