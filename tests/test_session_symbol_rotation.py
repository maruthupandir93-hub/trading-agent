"""A session scans several instruments in turn, one position at a time.

WHY. A session watched exactly ONE coin, and a single coin spends most of its
life doing nothing worth trading. Measured on the operator's live XRP/USDT
session over 35.5 hours:

    2,397 graph runs -> 3 trades (1 in 799)
    book FLAT for 65% of the window, with gaps of 11.5h and 4.5h between fills
    panel confidence on 46 of the last 50 refusals: 0.00-0.12 vs a 0.60 floor
    all three fills were Breakout — the three times XRP actually broke out

Nothing was broken. The bar is what made those three trades worth taking (+7.7%,
two wins of three), and lowering it buys entries in exactly the regime this
project's own backtest says the range strategies lose money in. The way to get
more trades is MORE INSTRUMENTS, not a lower bar.

WHAT THESE TESTS GUARD, in order of how much they would cost if wrong:

  1. every scanned symbol is TICK-SUBSCRIBED — a position whose instrument has
     no ticks has a stop that can never fire, and nothing reports it
  2. the rotation is COST-NEUTRAL — one graph run per interval, not one per coin
  3. one position at a time still holds, across ALL scanned symbols
  4. an untradeable symbol is REFUSED, not silently dropped
  5. the default is exactly the old single-coin behaviour
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.services.trading_session import TradingSession


def _session(**kw) -> TradingSession:
    base = dict(
        id="t1", symbol="XRP/USDT", leverage=10,
        start_equity=10_000.0, target_equity=15_000.0, floor_equity=5_000.0,
    )
    base.update(kw)
    return TradingSession(**base)


# ---------------------------------------------------------------------------
# 5. The default is the old behaviour
# ---------------------------------------------------------------------------

def test_a_session_with_no_list_scans_only_its_own_symbol():
    """Every existing session, every existing caller, and every restored row
    from before this field existed must behave exactly as before."""
    s = _session()
    assert s.watch_symbols == []
    assert s.scan_list() == ["XRP/USDT"]


def test_the_primary_symbol_is_always_first_and_never_duplicated():
    """`symbol` stays the session's identity — it is what the panel shows and
    what `start_session` refuses on. The rotation ADDS instruments to look at;
    it does not replace the one the operator chose."""
    s = _session(watch_symbols=["SOL/USDT", "XRP/USDT", "ETH/USDT"])
    assert s.scan_list() == ["XRP/USDT", "SOL/USDT", "ETH/USDT"]


# ---------------------------------------------------------------------------
# 2. Cost-neutral rotation
# ---------------------------------------------------------------------------

def test_the_rotation_visits_every_symbol_in_turn_and_wraps():
    """One graph run per decision interval, pointed somewhere different each
    time — NOT one run per coin per interval. Five coins must cost the same LLM
    budget and the same share of the 40/min rate limit as one."""
    s = _session(watch_symbols=["SOL/USDT", "ETH/USDT"])
    scan = s.scan_list()

    visited = []
    for _ in range(7):
        visited.append(scan[s.scan_index % len(scan)])
        s.scan_index = (s.scan_index + 1) % len(scan)

    assert visited == [
        "XRP/USDT", "SOL/USDT", "ETH/USDT",
        "XRP/USDT", "SOL/USDT", "ETH/USDT",
        "XRP/USDT",
    ]
    assert s.scan_index == 1, "the index must wrap, not grow without bound"


def test_the_loop_runs_the_graph_once_per_decision_not_once_per_symbol():
    """Asserted against the source, because the expensive mistake here is a
    `for symbol in scan:` around the decision — which would be 5x the spend for
    no extra edge. A breakout takes minutes to develop; checking each coin every
    ~4 minutes instead of every ~50 seconds misses nothing."""
    import backend.services.trading_session as ts

    src = inspect.getsource(ts._run_session)
    body = src[src.index("scan = session.scan_list()"):]
    decide = body.index("_decide_once(session, target)")
    assert "for " not in body[:decide], (
        "the decision must not be inside a loop over the scan list"
    )
    assert body.count("_decide_once") == 1


def test_analyses_run_still_counts_graph_runs_not_symbols():
    """The counter has to keep meaning the same thing or the funnel the operator
    reads becomes wrong in the flattering direction."""
    import backend.services.trading_session as ts

    src = inspect.getsource(ts._run_session)
    assert src.count("session.analyses_run += 1") == 1


# ---------------------------------------------------------------------------
# 3. One position at a time, across every scanned symbol
# ---------------------------------------------------------------------------

def test_a_position_in_ANY_scanned_symbol_stops_a_new_entry(monkeypatch):
    """With a rotation the open position may be in a DIFFERENT coin from the one
    about to be analysed. Checking only `session.symbol` would let the session
    reach the Risk Gateway with a second entry — which the gateway refuses, but
    only after a full 24-node run has been spent to arrive at a no."""
    import backend.services.trading_session as ts

    held = {"SOL/USDT"}

    async def _fake(symbol, tab):
        return symbol in held

    monkeypatch.setattr(ts, "_has_open_position", _fake)

    assert asyncio.run(ts._any_open_position(["XRP/USDT", "SOL/USDT"], "paper")) is True
    assert asyncio.run(ts._any_open_position(["XRP/USDT", "ETH/USDT"], "paper")) is False


def test_an_unreadable_book_blocks_a_new_entry(monkeypatch):
    """`_has_open_position` fails CLOSED — it returns True when it cannot tell.
    That must survive the wrapper, or an unreadable book becomes permission to
    open a second position."""
    import backend.services.trading_session as ts

    async def _boom(symbol, tab):
        return True  # what _has_open_position returns on an exception

    monkeypatch.setattr(ts, "_has_open_position", _boom)
    assert asyncio.run(ts._any_open_position(["XRP/USDT"], "paper")) is True


# ---------------------------------------------------------------------------
# 1. THE SAFETY-CRITICAL ONE — every scanned symbol gets ticks
# ---------------------------------------------------------------------------

def test_every_scanned_symbol_is_tick_subscribed():
    """A POSITION WITHOUT TICKS HAS A STOP THAT CAN NEVER FIRE.

    `PositionMonitorAgent` enforces every stop by reacting to TICK_RECEIVED and
    `live_market_data` is the ONLY publisher of that event. An unsubscribed
    instrument's `_check_price` never runs — while the monitor still lists the
    position as watched and the dashboard still shows its stop, so nothing
    anywhere reports a problem. CLAUDE.md records this bug happening once
    already with a hardcoded three-symbol list.

    A rotating session can open in any coin on its list, so subscribing only
    `session.symbol` reintroduces it for every other instrument.
    """
    import backend.services.live_market_data as lmd

    src = inspect.getsource(lmd)
    block = src[src.index("session = active_session()"):]
    block = block[:block.index("except") + 400]
    assert "session.scan_list()" in block, (
        "the subscription must cover every symbol the session may open in"
    )
    assert "wanted.add(session.symbol)" in block, (
        "and must still fall back to the primary symbol for a session restored "
        "from before scan_list existed"
    )


# ---------------------------------------------------------------------------
# 4. An untradeable symbol is refused, never dropped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_untradeable_extra_symbol_is_refused_not_silently_dropped(monkeypatch):
    """Dropping it would tell the operator the session covers five coins while
    it scanned four. Refusing names the problem they can act on.

    BTC is the live example: it is the benchmark every alt decision reads and is
    deliberately untradeable, so it would take its turn, run the full graph, and
    be refused at the gateway every single time — 55 such doomed runs were
    measured live before `start_session` began refusing them.
    """
    from backend.services import trading_session as ts

    # THE BLOCKLIST IS SET EXPLICITLY, because `tests/conftest.py` CLEARS it so
    # the many tests that use BTC as a generic symbol are not refused. Relying
    # on the production default here would make this test pass or fail on
    # another file's fixture rather than on the behaviour under test.
    # `tradeable_universe` reads the variable at CALL time, so no reset is
    # needed — that is the same property that lets an operator change it live.
    monkeypatch.setenv("UNTRADEABLE_SYMBOLS", "BTC/USDT")

    # The target must clear the CURRENT equity or that check refuses first and
    # this test passes for the wrong reason — which is exactly what it did on
    # the first run, against a 25,000 paper book.
    equity = await ts.current_equity(ts._tab_for_session())

    with pytest.raises(ValueError) as exc:
        await ts.start_session(
            symbol="XRP/USDT",
            symbols=["BTC/USDT"],
            leverage=3,
            target_equity=float(equity or 10_000.0) * 2.0,
        )
    message = str(exc.value).lower()
    assert "btc" in message, message
    assert "rotation" in message, message


def test_the_extra_list_drops_a_duplicate_of_the_primary():
    """Listing the primary again is a harmless operator slip, not an error — but
    it must not make that coin come up twice as often as the others."""
    s = _session(watch_symbols=["XRP/USDT", "SOL/USDT"])
    assert s.scan_list() == ["XRP/USDT", "SOL/USDT"]


# ---------------------------------------------------------------------------
# The log has to stay readable once several coins interleave
# ---------------------------------------------------------------------------

def test_each_decision_line_names_its_symbol():
    """With a rotation the log interleaves instruments, and "DO NOT TRADE: the
    Grid setup is LONG but the panel reads NEUTRAL at 0.05" is unreadable when
    the reader cannot tell which coin it was about. It was omitted before
    because a session had exactly one."""
    import backend.services.trading_session as ts

    src = inspect.getsource(ts._decide_once)
    assert 'f"[{target}] {action}' in src
    assert "symbol=target," in src
