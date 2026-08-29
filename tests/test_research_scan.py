"""The Research Agent's market scan: honest failures, and the right state key.

TWO BUGS THIS FILE EXISTS FOR, BOTH FOUND IN THE LIVE LOG
---------------------------------------------------------
1. THE SCAN NEVER DISCOVERED ANYTHING, EVER.

   `scan_market` read the multi-timeframe verdict as `mtf.get("overall", "Mixed")`.
   `run_multi_timeframe_analysis` does not return an `overall` key — it sets
   `features["multi_tf_trend"]`. So `.get()` always fell to its default, `trend`
   was permanently `"Mixed"`, and the two branches that set a setup
   ("Strong Long Setup" / "Strong Short Setup") were unreachable. Every scan
   returned five rows of `discovered_setup: "None"`.

   The visible symptom was a log line that reads like a market observation:

       Research Agent failed to find any setups.

   With the key corrected, the same five symbols immediately produced three
   "Strong Short Setup" rows against real per-symbol trends.

2. EVERY FAILURE PATH WAS SILENT OR EMPTY.

   `if resp.status_code == 200:` had no `else`, so a 451 (Binance refuses some
   regions), a 429, or anything else fell through to `return []` with no log at
   all — indistinguishable from a quiet market. And the one log line that did
   exist, `f"Error scanning market: {e}"`, printed as literally
   "Error scanning market: " because several httpx exceptions stringify to empty.
   Both appeared in the live log.

The scan is now routed through `services/upstream.fetch_json`, which is
geo-block aware and returns a stated reason rather than raising.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from backend.agents import research_agent
from backend.services.upstream import UpstreamResult


def run(coro):
    return asyncio.run(coro)


def _tickers(*symbols: str) -> List[Dict[str, Any]]:
    return [
        {
            "symbol": s,
            "quoteVolume": str(1_000_000 - i),
            "priceChangePercent": "-2.0",
        }
        for i, s in enumerate(symbols)
    ]


@pytest.fixture
def patched(monkeypatch):
    """Stub the two outbound calls. Returns a dict the test can steer."""
    box: Dict[str, Any] = {
        "result": UpstreamResult(data=_tickers("BTCUSDT", "ETHUSDT")),
        "mtf": {"multi_tf_trend": "Bearish"},
        "mtf_raises": None,
    }

    async def fake_fetch_json(url, **kwargs):
        return box["result"]

    async def fake_mtf(symbol):
        if box["mtf_raises"] is not None:
            raise box["mtf_raises"]
        return box["mtf"]

    monkeypatch.setattr("backend.services.upstream.fetch_json", fake_fetch_json)
    monkeypatch.setattr(research_agent, "run_multi_timeframe_analysis", fake_mtf)
    return box


# ---------------------------------------------------------------------------
# Bug 1 — the state key
# ---------------------------------------------------------------------------

def test_the_scan_reads_multi_tf_trend_and_can_actually_discover_a_setup(patched):
    """The regression that made the whole agent inert.

    A bearish multi-timeframe trend on a symbol that is DOWN on the day is the
    textbook "Strong Short Setup". Under the old `"overall"` key this returned
    "None" for every symbol on every scan.
    """
    rows = run(research_agent.scan_market())

    assert rows, "the scan returned nothing from a healthy feed"
    assert all(r["mtf_trend"] == "Bearish" for r in rows), rows
    assert all(r["discovered_setup"] == "Strong Short Setup" for r in rows), (
        "a bearish trend on a symbol down 2% must produce a short setup; getting "
        "'None' here means the trend key is being read from the wrong field again"
    )


def test_a_bullish_trend_on_a_rising_symbol_is_a_long_setup(patched):
    patched["mtf"] = {"multi_tf_trend": "Bullish"}
    patched["result"] = UpstreamResult(
        data=[{"symbol": "BTCUSDT", "quoteVolume": "5", "priceChangePercent": "3.1"}]
    )
    rows = run(research_agent.scan_market())
    assert rows[0]["discovered_setup"] == "Strong Long Setup"


def test_a_missing_trend_key_is_reported_as_unknown_not_silently_mixed(patched, caplog):
    """The default must not paper over a renamed key a second time.

    "Mixed" is a real verdict this analysis can return, so defaulting to it made
    a missing key indistinguishable from a genuinely mixed read — which is
    exactly how the original bug survived.
    """
    import logging

    patched["mtf"] = {"some_other_key": "Bullish"}
    with caplog.at_level(logging.WARNING):
        rows = run(research_agent.scan_market())

    assert rows[0]["mtf_trend"] == "Unknown"
    assert rows[0]["discovered_setup"] == "None"
    assert "multi_tf_trend" in caplog.text


# ---------------------------------------------------------------------------
# Bug 2 — failures must say why
# ---------------------------------------------------------------------------

def test_an_upstream_failure_is_logged_with_its_reason(patched, caplog):
    """A dead feed must never look like a quiet market."""
    import logging

    patched["result"] = UpstreamResult(data=None, status=451, error="region refused")
    with caplog.at_level(logging.ERROR):
        rows = run(research_agent.scan_market())

    assert rows == []
    assert "region refused" in caplog.text
    assert "FEED failure" in caplog.text, (
        "the log must distinguish a feed failure from an absence of setups"
    )


def test_a_geo_block_says_so(patched, caplog):
    import logging

    patched["result"] = UpstreamResult(
        data=None, status=451, error="HTTP 451", geo_blocked=True
    )
    with caplog.at_level(logging.ERROR):
        run(research_agent.scan_market())
    assert "refused this region" in caplog.text


def test_an_error_object_instead_of_a_list_does_not_raise(patched, caplog):
    """Binance returns an OBJECT on a rate limit, not a list.

    The old code did `d['symbol']` over it, raising
    `TypeError: string indices must be integers`, which the bare except then
    logged as the empty message from bug 2.
    """
    import logging

    patched["result"] = UpstreamResult(data={"code": -1003, "msg": "Too many requests"})
    with caplog.at_level(logging.ERROR):
        rows = run(research_agent.scan_market())

    assert rows == []
    assert "expected a list" in caplog.text


def test_rows_missing_their_numbers_are_dropped_not_defaulted(patched, caplog):
    """A volume of zero would sort to the bottom and read as a real illiquid pair."""
    import logging

    patched["result"] = UpstreamResult(data=[
        {"symbol": "GOODUSDT", "quoteVolume": "100", "priceChangePercent": "-1.0"},
        {"symbol": "BADUSDT", "quoteVolume": None, "priceChangePercent": "-1.0"},
        {"symbol": "ALSOBADUSDT"},
    ])
    with caplog.at_level(logging.WARNING):
        rows = run(research_agent.scan_market())

    assert [r["symbol"] for r in rows] == ["GOODUSDT"]


def test_one_symbols_analysis_failing_does_not_empty_the_whole_scan(patched, caplog):
    """A partial scan is useful; a silently shortened one is not."""
    import logging

    patched["mtf_raises"] = RuntimeError("klines timed out")
    with caplog.at_level(logging.WARNING):
        rows = run(research_agent.scan_market())

    assert rows == []
    assert "omitted from this scan" in caplog.text


def test_non_usdt_pairs_are_excluded(patched):
    patched["result"] = UpstreamResult(data=[
        {"symbol": "BTCUSDT", "quoteVolume": "100", "priceChangePercent": "-1.0"},
        {"symbol": "ETHBTC", "quoteVolume": "999", "priceChangePercent": "-1.0"},
    ])
    rows = run(research_agent.scan_market())
    assert [r["symbol"] for r in rows] == ["BTCUSDT"]


def test_results_are_ordered_by_volume_descending(patched):
    patched["result"] = UpstreamResult(data=[
        {"symbol": "SMALLUSDT", "quoteVolume": "10", "priceChangePercent": "-1.0"},
        {"symbol": "BIGUSDT", "quoteVolume": "9000", "priceChangePercent": "-1.0"},
        {"symbol": "MIDUSDT", "quoteVolume": "500", "priceChangePercent": "-1.0"},
    ])
    rows = run(research_agent.scan_market())
    assert [r["symbol"] for r in rows] == ["BIGUSDT", "MIDUSDT", "SMALLUSDT"]


def test_the_scan_is_capped_at_the_top_n(patched):
    patched["result"] = UpstreamResult(
        data=_tickers(*[f"SYM{i}USDT" for i in range(20)])
    )
    rows = run(research_agent.scan_market())
    assert len(rows) == research_agent.TOP_N_BY_VOLUME
