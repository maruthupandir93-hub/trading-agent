"""The P0 failures from the 2026-10-15 diagnosis, pinned.

F53 is the finding that makes this file necessary: the suite ran **2,243
passed** while F1, F21, F22, F23 and F24 were all present and the operator's
session had been frozen for four days. A green suite proves the paths it
exercises, not the ones it skips, and none of these had a test.

Each test here drives the INPUT to the decision rather than a happy path,
because every one of these bugs looks correct when read.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from backend.services.venue import SizingCheck


# ---------------------------------------------------------------------------
# F23 · reduce-only is exempt from the venue's minimum notional
# ---------------------------------------------------------------------------

def test_a_reduce_only_order_is_not_refused_by_the_minimum_notional():
    """INVARIANT 4. The venue exempts reduce-only orders from MIN_NOTIONAL
    precisely so a position whose value fell below it can still be closed. Our
    pre-check did not, so on a $2-$5 account:

        open 4.0 XRP at 1.28     notional $5.12, accepted
        stop 2.5% away           notional $4.99 at the stop price
        -> the resting stop is REFUSED at entry
        -> when the stop fires, every close is refused, on every tick

    The position could not exit.
    """
    from backend.services.venue import Venue

    v = Venue.__new__(Venue)
    v.id = "binance"

    async def _markets():
        return {"XRP/USDT:USDT": {"limits": {"amount": {"min": 0.1},
                                             "cost": {"min": 5.0}}}}

    async def _resolve(sym):
        return "XRP/USDT:USDT"

    class _Pub:
        @staticmethod
        def amount_to_precision(sym, qty):
            return f"{float(qty):.1f}"

    v.markets = _markets
    v.resolve_symbol = _resolve
    v.public = _Pub()

    # $4.99 of notional: below the $5 floor, above the 0.1 minimum quantity.
    opening = asyncio.run(v.check_size("XRP/USDT", 3.9, 1.28))
    assert opening.ok is False, "an OPENING order below the minimum must still be refused"
    assert "minimum" in (opening.reason or "")

    closing = asyncio.run(v.check_size("XRP/USDT", 3.9, 1.28, reduce_only=True))
    assert closing.ok is True, (
        "a reduce-only close below the minimum notional must be allowed through; "
        "refusing it makes the position unexitable"
    )


def test_a_reduce_only_order_is_still_refused_below_the_minimum_QUANTITY():
    """Only the NOTIONAL floor is lifted. The minimum quantity and the step are
    what the venue can represent at all, and an order below them is rejected
    reduce-only or not -- so passing one would just move the refusal to the
    exchange, which is worse because it costs a round trip to find out."""
    from backend.services.venue import Venue

    v = Venue.__new__(Venue)
    v.id = "binance"
    v.markets = lambda: _as_coro({"XRP/USDT:USDT": {
        "limits": {"amount": {"min": 5.0}, "cost": {"min": 5.0}}}})
    v.resolve_symbol = lambda sym: _as_coro("XRP/USDT:USDT")

    class _Pub:
        @staticmethod
        def amount_to_precision(sym, qty):
            return f"{float(qty):.1f}"

    v.public = _Pub()

    out = asyncio.run(v.check_size("XRP/USDT", 1.0, 1.28, reduce_only=True))
    assert out.ok is False
    assert "minimum quantity" in (out.reason or "")


def _as_coro(value):
    async def _inner():
        return value
    return _inner()


def test_both_protective_legs_are_placed_as_reduce_only_sizes():
    """The stop and the take-profit are reduce-only by construction, so neither
    may be judged against the opening minimum. Asserted against the source
    because driving them needs a venue."""
    from backend.services import venue as venue_mod

    for fn_name in ("place_stop_loss", "place_take_profit"):
        src = inspect.getsource(getattr(venue_mod.Venue, fn_name))
        assert "reduce_only=True" in src, f"{fn_name} must size as a reduce-only order"


# ---------------------------------------------------------------------------
# F24 · a tightened stop must never leave two, or none, at the venue
# ---------------------------------------------------------------------------

def test_every_protective_placement_gets_a_FRESH_client_id():
    """Reusing `sl_{tar_id}` breaks differently on each venue (F24):

      * Bybit needs `orderLinkId` unique across ALL orders ever, so the first
        tighten cancelled the old stop and had the new one REFUSED -- leaving
        NO stop resting at all.
      * Binance needs it unique among OPEN orders, so a racing replacement
        could leave an order resting that the monitor no longer has the id for.
    """
    from backend.agents.position_monitor import PositionMonitorAgent, _Tracked

    mon = PositionMonitorAgent.__new__(PositionMonitorAgent)
    mon._protective_seq = {}
    pos = _Tracked(tar_id=uuid.uuid4().hex)

    ids = {mon._protective_id("sl", pos) for _ in range(5)}
    assert len(ids) == 5, f"every placement needs its own id, got {ids}"
    assert all(len(i) <= 36 for i in ids), "Binance caps the client id at 36 characters"
    assert all(i.startswith("sl_") for i in ids)

    # And a stop's id can never collide with its own take-profit's.
    tp = mon._protective_id("tp", pos)
    assert tp not in ids


def test_a_second_replacement_does_not_start_while_one_is_in_flight():
    """COALESCED. The trail tightens on every new peak, so a fast move queued a
    cancel/place pair per tick and they raced each other. One in flight is
    enough -- it re-reads `pos.stop_loss` after taking the lock, so it carries
    the newest level rather than the one that scheduled it."""
    from backend.agents.position_monitor import PositionMonitorAgent

    src = inspect.getsource(PositionMonitorAgent._replace_resting_stop_soon)
    assert "_stop_replacing" in src, "a replacement already in flight must suppress the next"
    assert "add_done_callback" in src, "the task must be strongly referenced until it finishes"

    body = inspect.getsource(PositionMonitorAgent._replace_resting_stop)
    assert "_stop_locks" in body and "async with lock" in body, (
        "cancel-then-place must be serialised per position"
    )
    # The level is re-read AFTER the lock, not captured before it.
    assert body.index("async with lock") < body.rindex("pos.stop_loss is None")


# ---------------------------------------------------------------------------
# F22 · slow work must not sit on the tick's causal chain
# ---------------------------------------------------------------------------

def test_the_analysis_run_is_detached_from_the_trigger():
    """`MessageBus.publish` drains a causal chain inside the ORIGINAL
    publisher's await, and that publisher is `live_market_data`'s tick watcher.
    While `on_trigger` awaited a 39-second graph run, no TICK_RECEIVED was
    published for the symbol -- so `_check_price` never ran and no stop could
    fire for the whole run."""
    from backend.graphs import analysis

    src = inspect.getsource(analysis.subscribe_to_triggers)
    assert "create_task" in src, "the graph run must not be awaited on the tick's chain"
    assert "_running_analyses" in src, "one run per symbol, or a burst multiplies LLM spend"
    assert "await run_analysis_graph(" in src, "the run itself is still awaited, inside the task"
    # The await must live in the detached body, not in the handler.
    handler_start = src.index("async def on_trigger")
    detached_start = src.index("async def _analyse")
    assert src.index("await run_analysis_graph(") > detached_start > handler_start


def test_the_reflection_is_detached_but_the_count_is_not():
    """The count is a FACT that every reader of the win rate depends on, makes
    no model call and never raises, so it stays inline. The reflection runs on
    the REASONING tier with a 300 s timeout, so it cannot."""
    from backend.agents.reflection_agent import ReflectionAgent

    src = inspect.getsource(ReflectionAgent.handle_event)
    assert "await record_closed_trade(" in src
    assert "create_task(self._reflect_on_close_guarded(" in src
    assert "await self._reflect_on_close(" not in src


# ---------------------------------------------------------------------------
# F1 · "the venue is already flat" is a normal outcome, not an error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("held,expected", [
    ({}, True),                                            # genuinely flat
    ({"symbol": "XRP/USDT", "qty": 20.1}, False),          # still holding
    (None, None),                                          # could not ask
])
def test_venue_flat_keeps_could_not_ask_separate_from_flat(monkeypatch, held, expected):
    """THE THREE-VALUED ANSWER IS THE WHOLE POINT. A timeout that read as
    "flat" would make the caller forget a position the venue still holds,
    abandoning a real order with no stop behind it -- strictly worse than the
    retry loop this exists to end."""
    from backend.services import paper_testnet

    class _V:
        @staticmethod
        async def open_positions(symbol):
            return held

    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    monkeypatch.setattr(paper_testnet, "get_venue", lambda: _V())
    assert asyncio.run(paper_testnet.venue_flat("XRP/USDT")) is expected


def test_an_unreadable_venue_is_not_reported_as_flat(monkeypatch):
    """A raising reader is "could not ask", never "nothing is there"."""
    from backend.services import paper_testnet

    class _V:
        @staticmethod
        async def open_positions(symbol):
            raise RuntimeError("timeout")

    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    monkeypatch.setattr(paper_testnet, "get_venue", lambda: _V())
    assert asyncio.run(paper_testnet.venue_flat("XRP/USDT")) is None


def test_a_flat_venue_settles_the_book_instead_of_retrying_forever():
    """THE FOUR-DAY FREEZE, as a test.

    A mirrored close that the venue refuses used to `return None` for every
    reason, and the caller reads None as "did not close, retry next tick". When
    the refusal is "there is no position", that retry can never succeed: the
    resting stop had already closed it. The live session sent a signed order
    every twelve seconds for four days and never decided again.
    """
    from backend.agents.execution_agent import ExecutionAgent

    src = inspect.getsource(ExecutionAgent.close_position)
    assert "venue_flat(" in src, "a refused mirrored close must ask whether the venue is flat"
    assert "flat is not True" in src, (
        "only a definite True may settle the book -- False and None must both retry"
    )
    assert "exit_fill(" in src, (
        "settle at the venue's own fill where it can be read; the trigger price "
        "is a different number and the gap is real money"
    )


def test_the_close_failure_alert_escalates_exactly_once():
    """A retry that never succeeds must stop being quiet. It logged at ERROR on
    every tick and nothing escalated, so four days of failure looked the same
    as one."""
    from backend.agents import position_monitor as pm

    src = inspect.getsource(pm.PositionMonitorAgent._close)
    assert "close_failures" in src
    assert "logger.critical(" in src
    assert "== _CLOSE_FAILURE_ALERT" in src, (
        "fire on the threshold crossing, not on every tick past it, or the "
        "alert drowns in the same way the ERROR did"
    )
    assert pm._CLOSE_FAILURE_ALERT > 1


# ---------------------------------------------------------------------------
# F7 · a mirrored paper position has REAL orders and must be observed
# ---------------------------------------------------------------------------

def test_venue_observation_follows_the_orders_not_the_live_flag(monkeypatch):
    """`order_stream` and `reconciliation` both ran only with LIVE_TRADING on.
    A mirrored paper position holds a real resting stop and take-profit, so
    every disagreement between the venue and the book was invisible -- which is
    the whole of F1."""
    from backend.core.config import settings
    from backend.services import order_stream, paper_testnet

    # `LIVE_TRADING` is a read-at-call-time PROPERTY (deliberately -- see the
    # `simulation_mode` note), so the backing field is what a test sets.
    monkeypatch.setattr(settings, "_live_trading", False, raising=False)

    monkeypatch.setattr(paper_testnet, "active", lambda: False)
    assert order_stream.venue_backed_book_exists() is False

    monkeypatch.setattr(paper_testnet, "active", lambda: True)
    assert order_stream.venue_backed_book_exists() is True, (
        "an active mirror means real orders are resting at a venue"
    )

    monkeypatch.setattr(settings, "_live_trading", True, raising=False)
    monkeypatch.setattr(paper_testnet, "active", lambda: False)
    assert order_stream.venue_backed_book_exists() is True


def test_reconciliation_asks_the_same_question(monkeypatch):
    from backend.services import reconciliation

    src = inspect.getsource(reconciliation)
    assert "venue_backed_book_exists()" in src, (
        "reconciliation must observe a mirrored book too, or F1 is invisible again"
    )


# --------------------------------------------------------------------------
# F11: the panel must reason about the REAL order book, not the demo one.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_microstructure_reads_mainnet_even_while_the_demo_mirror_is_on(monkeypatch):
    """The depth and tape fetch must not follow `demo_data_active()`.

    Prices and candles SHOULD follow it -- fills happen at the demo venue. The
    order book must not: Binance's demo book is thin and synthetic, and the
    orderflow + liquidity specialists reading it carry 3.0 of the debate's 7.0
    weight. Before this, turning the paper mirror on silently swapped the
    market those two voted about.
    """
    from backend.services import binance_testnet, microstructure_feed

    monkeypatch.setattr(binance_testnet, "demo_data_active", lambda: True)
    microstructure_feed._micro_cache.clear()

    urls: list[str] = []

    async def _fake_fetch_json(url, *, label=""):
        urls.append(url)
        from backend.services.upstream import UpstreamResult
        if "depth" in url:
            return UpstreamResult(data={"bids": [["100", "1"]], "asks": [["101", "1"]]})
        return UpstreamResult(data=[])

    monkeypatch.setattr(microstructure_feed, "fetch_json", _fake_fetch_json)

    await microstructure_feed.fetch_microstructure("SOL/USDT", force=True)

    assert urls, "no upstream call was made"
    for url in urls:
        assert url.startswith(microstructure_feed.BINANCE_SPOT), url
        assert binance_testnet.BASE_URL not in url, (
            f"microstructure fetched from the demo venue: {url}"
        )
