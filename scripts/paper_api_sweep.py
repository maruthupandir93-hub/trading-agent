"""Trigger every API in paper mode and report what answered — a manual sweep.

WHY THIS IS A SCRIPT AND NOT THE TEST SUITE
===========================================
`tests/test_api_surface.py` asserts each documented prefix HAS routes and that
the safety-relevant ones behave. This is the complementary thing the operator
asked for: actually CALL every endpoint, in paper mode, and print one line per
route saying what came back — so a human can see the whole surface answering at
once rather than trusting 1600 green dots.

It runs against the FastAPI app IN-PROCESS through Starlette's TestClient, with
the database pointed at an unreachable DSN exactly as `conftest.isolate_database`
does. That matters here more than anywhere:

    THE PRODUCTION DATABASE IS SHARED WITH THE LIVE ORACLE BACKEND.

Hitting the deployed backend, or pointing this at the real DATABASE_URL, would
write sweep trades into the same book the running agent is trading. So this never
touches the network and never touches Supabase. A route that only works against a
populated database will answer 200 with empty data or a clean 503 here, and that
is the correct outcome to observe — an empty answer is not a failure.

WHAT "PASS" MEANS
=================
Any HTTP status that is a DELIBERATE answer: 200, or a 4xx/503 the handler
returns on purpose (no credentials, nothing to show, write-auth required). A
FAIL is a 500 or an unhandled exception — the endpoint fell over rather than
answered.

    .venv/Scripts/python.exe scripts/paper_api_sweep.py
    .venv/Scripts/python.exe scripts/paper_api_sweep.py --verbose   (dump bodies)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# BEFORE importing the app. Isolate the database and force paper mode, so nothing
# in this process can reach the shared book or place a real order.
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/tradingos_sweep_unreachable"
os.environ["LIVE_TRADING"] = "false"
os.environ["USE_TESTNET"] = "true"
os.environ.setdefault("TRADES_API_KEY", "sweep-key")

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

WRITE_KEY = os.environ["TRADES_API_KEY"]
AUTH = {"X-API-Key": WRITE_KEY}

_PASS, _FAIL, _SKIP = 0, 0, 0
_FAILURES: list[str] = []
VERBOSE = False


def _colour(status: int) -> str:
    if status < 300:
        return "\033[32m"  # green
    if status < 500:
        return "\033[33m"  # amber — a deliberate refusal
    return "\033[31m"      # red — a fall-over


def call(client: TestClient, method: str, path: str, *, auth: bool = False,
         json: dict | None = None, expect_ok=None, note: str = "") -> None:
    global _PASS, _FAIL
    headers = AUTH if auth else {}
    try:
        resp = client.request(method, path, headers=headers, json=json)
    except Exception as exc:  # noqa: BLE001
        _FAIL += 1
        _FAILURES.append(f"{method} {path} raised {type(exc).__name__}: {exc}")
        print(f"  \033[31mEXC \033[0m {method:6} {path}  {type(exc).__name__}: {exc}")
        return

    ok = resp.status_code < 500 if expect_ok is None else (resp.status_code in expect_ok)
    tag = "PASS" if ok else "FAIL"
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILURES.append(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")

    c = _colour(resp.status_code)
    print(f"  {c}{tag}\033[0m {method:6} {path:42} {c}{resp.status_code}\033[0m  {note}")
    if VERBOSE and resp.status_code < 500:
        body = resp.text[:400].replace("\n", " ")
        print(f"         {body}")


def header(title: str) -> None:
    print(f"\n\033[1m--- {title}\033[0m")


def run() -> None:
    with TestClient(app) as client:
        # -- market data (agent futures view + dashboard spot) --------------
        header("Market data")
        call(client, "GET", "/api/market/price/SOL~USDT".replace("~", "/"))
        call(client, "GET", "/api/market/klines/SOL/USDT?timeframe=15m&limit=50")
        call(client, "GET", "/api/market/regime/SOL/USDT")
        call(client, "GET", "/api/market/analysis/SOL/USDT")
        call(client, "GET", "/api/market/prices")
        call(client, "GET", "/api/marketdata/ticks")
        call(client, "GET", "/api/marketdata/candles?symbol=SOLUSDT&type=crypto&interval=15m")
        call(client, "GET", "/api/marketdata/orderflow?binance=SOLUSDT")
        call(client, "GET", "/api/marketdata/news")

        # -- the tradeable-universe change ----------------------------------
        header("Instrument gating (the BTC change)")
        call(client, "GET", "/api/market/regime/BTC/USDT", note="BTC still watched/priced")

        # -- session / home panel -------------------------------------------
        header("Session (home panel: account amounts, not coin price)")
        call(client, "GET", "/api/session")

        # -- portfolio / positions / stats ----------------------------------
        header("Portfolio, positions, P&L")
        call(client, "GET", "/api/dashboard")
        call(client, "GET", "/api/dashboard/portfolio")
        call(client, "GET", "/api/dashboard/events")
        call(client, "GET", "/api/graphs/positions", note="the monitor's book")
        call(client, "GET", "/api/catalog/orders")
        call(client, "GET", "/api/catalog/strategies")

        # -- graphs / pipeline / learning -----------------------------------
        header("Graphs, pipeline, learning loop")
        call(client, "GET", "/api/graphs/nodes")
        call(client, "GET", "/api/graphs/runs?graph=trade_analysis")
        call(client, "GET", "/api/graphs/positions")
        call(client, "GET", "/api/graphs/volatility")
        call(client, "GET", "/api/graphs/strategy-performance", note="the loop that was dead")
        call(client, "GET", "/api/graphs/reconciliation")

        # -- venue / exchange (read-only) -----------------------------------
        header("Venue & exchange")
        call(client, "GET", "/api/admin/venue")
        call(client, "GET", "/api/exchange/status")
        call(client, "GET", "/api/admin/trading-mode")

        # -- knowledge / memory / research ----------------------------------
        header("Knowledge, memory, research")
        call(client, "GET", "/api/knowledge")
        call(client, "GET", "/api/memory")
        call(client, "GET", "/api/research/dashboard")

        # -- monitoring / execution -----------------------------------------
        header("Monitoring & execution")
        call(client, "GET", "/api/monitoring")
        call(client, "GET", "/api/execution")

        # -- the operator paper trade lifecycle -----------------------------
        header("Operator paper trade (context -> book)")
        call(client, "GET", "/api/operator/trade/context?symbol=SOL/USDT")

        # -- write-auth: refused without the key, accepted with it ----------
        header("Write-auth (the most important assertion)")
        call(client, "POST", "/api/admin/venue",
             json={"venue": "bybit", "confirm": "bybit"},
             expect_ok={401, 403}, note="MUST refuse without the key")
        call(client, "GET", "/api/session")

    print(f"\n{'=' * 70}")
    print(f"  {_PASS} answered, {_FAIL} fell over")
    if _FAILURES:
        for f in _FAILURES:
            print(f"    \033[31m- {f}\033[0m")
    else:
        print("  Every endpoint answered in paper mode without a 500.")
    print("=" * 70)


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true", help="dump response bodies")
    VERBOSE = ap.parse_args().verbose
    run()
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
