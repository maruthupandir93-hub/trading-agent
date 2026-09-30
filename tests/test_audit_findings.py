"""The defects an external audit found that this suite did not, each pinned.

An audit run in September 2026 reported 20 failed assertions against a suite
that was passing 2,059 tests. Several assertions were variants of one defect, so
they are not 20 bugs — but the ones below were confirmed against the source
before anything was changed, and every one of them is the same SHAPE of bug:

    a value that is carried to the decision point and then not used

`close_position` takes `tab` and branched on a global flag. The portfolio
specialist takes the execution tab from settings and hardcoded "paper". The
portfolio restore reads `side` defensively from a SELECT that never asked for
it. The supervisor's exit reads a direction that the book stores in a field it
never looks at. The testnet mirror is given a filled quantity and substitutes
the requested one. In each case the code looks like it is using the right input,
which is why a green suite could sit on top of them.

These tests are therefore written against the INPUT reaching the decision, not
against a happy path.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest


# ---------------------------------------------------------------------------
# CRITICAL 1 — execution routes by the position's book, not the global flag
# ---------------------------------------------------------------------------

def test_routing_needs_both_a_real_tab_and_live_trading():
    """Each term guards a different way of placing an order that should not exist.

    `tab == "real"` stops a paper position reaching a venue at all.
    `not simulation_mode` stops the backtest engine reaching one however a
    position happens to be labelled.
    """
    from backend.agents.execution_agent import ExecutionAgent

    live = ExecutionAgent(simulation_mode=False)
    assert live.routes_to_venue("real") is True
    assert live.routes_to_venue("paper") is False

    pinned = ExecutionAgent(simulation_mode=True)
    assert pinned.routes_to_venue("real") is False
    assert pinned.routes_to_venue("paper") is False


def test_a_paper_close_stays_simulated_after_live_trading_is_switched_on():
    """THE CRITICAL PATH, DRIVEN RATHER THAN READ.

    The sequence that produced a real mainnet order from a paper stop:

        a PAPER position is opened while LIVE_TRADING is off
        the operator turns LIVE_TRADING on
        the monitor's stop fires on that paper position
        -> the close took the live branch, because the flag had flipped

    Reduce-only bounds the damage — it cannot OPEN anything — but it is not
    harmless: if the operator holds a real position in the same symbol, that
    order closes part of the REAL one to satisfy a paper stop, while the paper
    book books its own simulated close. Both books move on one event and neither
    is right.
    """
    from backend.agents.execution_agent import ExecutionAgent
    from backend.core.config import settings

    agent = ExecutionAgent(simulation_mode=None)
    agent._last_prices["XRP/USDT"] = 1.50

    reached_venue = []

    async def _boom():
        reached_venue.append(True)
        raise AssertionError("a paper close must never build a mainnet client")

    import backend.services.venue as venue_mod

    original = venue_mod.get_venue
    venue_mod.get_venue = lambda: asyncio.run(_boom())
    settings._live_trading = True
    booked = []

    async def _fake_book(**kw):
        booked.append(kw)
        return {"ok": True, "realized": 0.0}

    agent._apply_paper_fill = _fake_book
    try:
        assert settings.LIVE_TRADING is True
        fill = asyncio.run(agent.close_position(
            symbol="XRP/USDT", entry_side="buy", qty=10.0, tab="paper",
            reason="stop-loss", observed_price=1.49,
        ))
    finally:
        venue_mod.get_venue = original
        settings._live_trading = False

    assert not reached_venue, "the paper close reached the mainnet venue layer"
    assert fill == 1.49
    assert booked and booked[0]["reduce_only"] is True


# ---------------------------------------------------------------------------
# CRITICAL 2 — a price is not a close
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, ok=True, price=1.5, filled=None, error=None):
        self.ok = ok
        self.average_price = price
        self.filled_qty = filled
        self.error = error
        self.order_id = "live-1"
        self.raw = {"id": "live-1"}
        self.adjusted_qty = None
        self.requested_qty = None


class _LiveVenue:
    id = "bybit"

    def __init__(self, result):
        self._result = result

    async def market_order(self, **kw):
        return self._result


def _live_close(monkeypatch, result, qty=100.0):
    from backend.agents.execution_agent import ExecutionAgent
    import backend.services.venue as venue_mod

    agent = ExecutionAgent(simulation_mode=False)
    monkeypatch.setattr(venue_mod, "get_venue", lambda: _LiveVenue(result))
    return asyncio.run(agent.close_position(
        symbol="XRP/USDT", entry_side="buy", qty=qty, tab="real", reason="stop-loss",
    ))


@pytest.mark.parametrize("filled,label", [
    (0.0, "an accepted order that filled nothing"),
    (30.0, "an accepted order that filled 30 of 100"),
    (None, "an accepted order with no filled quantity reported"),
])
def test_a_close_that_did_not_fill_is_not_reported_as_closed(monkeypatch, filled, label):
    """The caller reads a non-None return as "the position is flat".

    `position_monitor._close` deletes the watch row, cancels the resting stop and
    take-profit, publishes POSITION_CLOSED and books a realized P&L on the
    strength of this return. On a shortfall all of that happened while the
    residual stayed open at the exchange with NOTHING enforcing its stop — the
    exact failure the resting stop exists to prevent, reached by reporting
    success.

    None is the retryable answer, and `reduce_only=True` is what makes the retry
    safe: it can only ever shrink what is actually there.
    """
    assert _live_close(monkeypatch, _Result(filled=filled)) is None, label


def test_a_complete_close_still_returns_its_fill(monkeypatch):
    """The tolerance is relative, because a venue's step size legitimately trims
    the last fraction and an exact-equality test would call every rounded close a
    partial one."""
    assert _live_close(monkeypatch, _Result(price=1.52, filled=100.0)) == 1.52
    assert _live_close(monkeypatch, _Result(price=1.52, filled=99.99)) == 1.52


# ---------------------------------------------------------------------------
# HIGH — the portfolio specialist reads the book that is trading
# ---------------------------------------------------------------------------

def test_the_portfolio_specialist_follows_the_execution_tab():
    """It is the ONLY writer of `portfolio_state`, which the Supervisor (Phase 27)
    and the Risk Gateway (Phase 28) both read and which stamps the plan's tab.

    Hardcoded to "paper", every real-money decision was made against the paper
    book: the "already holding this" constraint checked the wrong positions, and
    the equity the gateway sizes a fraction of was the wrong account's.
    """
    src = inspect.getsource(
        __import__("backend.graphs.nodes.specialists", fromlist=["x"]).specialist_portfolio
    )
    assert 'tab = "paper"' not in src
    assert "settings.execution_tab" in src


def test_the_specialists_equity_is_not_the_1x_only_formula():
    """`cash + notional` is the formula this project already removed from
    `book_equity` and from the TypeScript side, and it survived here.

    Cash is FREE cash — the margin was deducted at open — so adding the full
    notional back double-counts the leveraged part. At 10x a $900 position funded
    by $100 reported $1,900 of equity on a $1,000 account, and this figure is
    what the Risk Gateway sizes a fraction of.
    """
    src = inspect.getsource(
        __import__("backend.graphs.nodes.specialists", fromlist=["x"]).specialist_portfolio
    )
    assert "float(cash) + held_notional" not in src
    assert "held_margin" in src


# ---------------------------------------------------------------------------
# HIGH — a restored short is a short
# ---------------------------------------------------------------------------

def test_the_portfolio_restore_asks_for_the_side_column():
    """The row builder reads `side` defensively — `if "side" in r.keys()` — so a
    SELECT that never named the column silently took the "buy" default on every
    row, and every stored SHORT came back from a restart as a LONG.

    Same shape as the `TarApprovedEvent` fields that were passed but never
    declared: a defensive read of a value that does not arrive is
    indistinguishable from a legitimate absence, so nothing reported a problem.
    """
    src = inspect.getsource(
        __import__("backend.services.portfolio_store", fromlist=["x"]).load_portfolio
    )
    select = src[src.index("FROM agent_positions"):]
    assert "side" in src[:src.index("FROM agent_positions")].rsplit("SELECT", 1)[-1], (
        "the SELECT must name `side`, or the defensive read below always defaults"
    )
    assert select  # the query is one statement; the assertion above is the point


# ---------------------------------------------------------------------------
# HIGH — a short exits on an opposing LONG
# ---------------------------------------------------------------------------

def test_a_short_is_recognised_from_its_side_not_the_sign_of_its_quantity():
    """The book stores a POSITIVE quantity plus an explicit `side`, added because
    it could not represent a short at all otherwise. Reading `qty > 0` as LONG
    made every short read as a long, so `opposing` came out SHORT and the
    confident LONG verdict that should close a short was discarded.

    The effect was one-sided and therefore invisible from the winning side: longs
    exited on an opposing view exactly as designed, shorts never did. They could
    still be closed by a stop or target; they simply stopped being closable by
    the analysis changing its mind, which is this branch's whole purpose.
    """
    src = inspect.getsource(__import__("backend.graphs.nodes.supervisor", fromlist=["x"]))
    body = src[src.index("held_side = ") - 2000:src.index("held_side = ")]
    assert 'pos.get("side")' in body, (
        "direction must come from the stored side, not the sign of the quantity"
    )


# ---------------------------------------------------------------------------
# HIGH — an unfilled testnet order is not a fill
# ---------------------------------------------------------------------------

class _MirrorVenue:
    def __init__(self, result):
        self._result = result
        self.orders = []

    async def ensure_leverage(self, symbol, leverage):
        return True

    async def market_order(self, **kw):
        self.orders.append(kw)
        return self._result


class _MirrorResult:
    def __init__(self, ok=True, price=1.5, filled=None):
        self.ok = ok
        self.average_price = price
        self.filled_qty = filled
        self.order_id = "tn-9"
        self.error = None


@pytest.mark.parametrize("filled", [0.0, None])
def test_an_unfilled_testnet_order_falls_back_instead_of_claiming_the_full_size(
    monkeypatch, filled
):
    """`float(filled) if filled else float(qty)` treated 0.0 and None as falsy and
    substituted the REQUESTED quantity — so an order that was accepted and filled
    nothing came back as a complete fill of everything asked for.

    That is precisely the flattering execution this module exists to remove. The
    operator turns the mirror on to see real slippage, partial fills and refused
    sizes; the one case that is neither a fill nor a refusal was rounded up into
    a perfect one.
    """
    import backend.services.paper_testnet as pt

    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "s")
    monkeypatch.setenv(pt.ENV_VAR, "true")
    monkeypatch.setattr(pt, "get_venue", lambda: _MirrorVenue(_MirrorResult(filled=filled)))

    assert asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=10.0)) is None


def test_a_partial_testnet_fill_is_carried_through_at_its_real_size(monkeypatch):
    """The other half of the same rule: a genuine partial must NOT be rounded up
    either. It is one of the real costs the mirror exists to surface."""
    import backend.services.paper_testnet as pt

    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BYBIT_TESTNET_SECRET", "s")
    monkeypatch.setenv(pt.ENV_VAR, "true")
    monkeypatch.setattr(pt, "get_venue", lambda: _MirrorVenue(_MirrorResult(filled=4.0)))

    out = asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=10.0))
    assert out["filled_qty"] == 4.0


# ---------------------------------------------------------------------------
# HIGH — a refused book write must not become a fill event
# ---------------------------------------------------------------------------

def test_the_open_path_checks_whether_the_book_accepted_the_fill():
    """`apply_paper_fill` refuses an open the account cannot fund. That refusal
    was logged and then ignored: the row was already in `trades`, ORDER_FILLED
    went out, the monitor began watching a position, and the paper book held
    nothing — three components disagreeing about whether a position existed, and
    the one holding the MONEY was the one that said no.
    """
    from backend.agents.execution_agent import ExecutionAgent

    src = inspect.getsource(ExecutionAgent._execute_tar)
    assert "booked = await self._apply_paper_fill" in src
    book_at = src.index("booked = await self._apply_paper_fill")
    publish_at = src.index("OrderFilledEvent(")
    assert "if not booked:" in src[book_at:publish_at], (
        "the refusal must be checked between the book write and ORDER_FILLED"
    )


def test_apply_paper_fill_distinguishes_a_refusal_from_a_successful_open():
    """It returned `Optional[float]` — the realized P&L — so `None` meant BOTH
    "the book refused this" and "this was an open, which has no realized P&L".
    A caller could not tell the two apart, which is why the refusal could be
    ignored without anyone writing a bug."""
    from backend.agents.execution_agent import ExecutionAgent

    sig = inspect.signature(ExecutionAgent._apply_paper_fill)
    assert "Dict" in str(sig.return_annotation)


# ---------------------------------------------------------------------------
# MEDIUM — `exchange_order_id` actually discriminates now
# ---------------------------------------------------------------------------

def test_a_simulated_fill_writes_no_exchange_order_id():
    """THE PROPERTY WAS ASSERTED IN FOUR PLACES AND TRUE IN NONE.

    `paper_testnet`'s docstring (safety property 4), CLAUDE.md, the Settings
    panel's footer and this file all told the operator that
    `trades.exchange_order_id` is NULL when no venue order stood behind a row.
    `order_id` is a uuid4 minted at the top of `_execute_tar` and only REPLACED
    when a venue returns its own — so it was non-NULL on every row.

    That is the field you reach for to answer "was this paper result real?", and
    a documented discriminator that does not discriminate is worse than an
    undocumented one.
    """
    from backend.agents.execution_agent import ExecutionAgent

    src = inspect.getsource(ExecutionAgent._execute_tar)
    assert "venue_backed = to_venue" in src
    assert "(order_id if venue_backed else None)" in src
    # And the flag is raised by a mirrored fill, which IS venue-backed.
    mirrored_at = src.index("if mirrored:")
    assert "venue_backed = True" in src[mirrored_at:mirrored_at + 400]


# ---------------------------------------------------------------------------
# HIGH — a graph-driven close runs the whole close sequence
# ---------------------------------------------------------------------------

def test_the_execution_service_closes_through_the_monitor_when_it_is_watching():
    """`close_position` places the order and settles the book and does NOTHING
    ELSE. The closed-trade row carrying the realized pnl, the watch row's
    deletion, the resting stop/take-profit cancels and POSITION_CLOSED — which
    drives reflection, the learning ledger and the Telegram alert — all live in
    `PositionMonitorAgent._close`.

    Reproduced by the audit: a graph-driven exit left the paper book flat, one
    STALE row in `monitored_positions`, and zero POSITION_CLOSED events. The
    stale row is not inert — `may_open_new_position` counts it — so the slot the
    exit was taken to free stayed occupied, and the trade produced no lesson.

    THE DIRECT CALL REMAINS AS THE FALLBACK. A position the monitor is not
    tracking still has to be closable; invariant 4 makes no exception for a
    bookkeeping gap.
    """
    from backend.services.execution_service import ExecutionService

    src = inspect.getsource(ExecutionService._close)
    assert "monitor.close_tracked(" in src
    assert src.index("monitor.close_tracked(") < src.index("self._agent.close_position("), (
        "the monitor must be tried first; the direct call is the fallback"
    )


def test_close_tracked_refuses_rather_than_inventing_a_price():
    """Invariant 6. A close needs a price to book a realized P&L against, and a
    fabricated one would be recorded as the trade's result."""
    from backend.agents.position_monitor import PositionMonitorAgent

    src = inspect.getsource(PositionMonitorAgent.close_tracked)
    assert "return None" in src
    assert "no observed price" in src


def test_close_tracked_on_an_untracked_symbol_is_a_miss_not_a_crash():
    from backend.agents.position_monitor import PositionMonitorAgent

    agent = PositionMonitorAgent()
    assert asyncio.run(agent.close_tracked("XRP/USDT", "thesis-invalidated")) is None


# ---------------------------------------------------------------------------
# HIGH — the live-trading gate checks the venue that places the orders
# ---------------------------------------------------------------------------

def test_enabling_live_trading_checks_the_configured_venues_credentials():
    """It asked `exchange_client`, the older Binance-only client, and named
    BINANCE_API_KEY in the refusal. The agent's real order path is
    `services/venue`, whose credentials are PER VENUE.

    Both directions were wrong and one is dangerous: a fully configured Bybit
    deployment was refused for the absence of Binance keys, and — the mirror of
    that — leftover Binance keys would have PASSED the check on a Bybit
    deployment with no Bybit keys at all.
    """
    src = inspect.getsource(
        __import__("backend.api.admin", fromlist=["x"]).enable_live_trading
    )
    assert "get_exchange_client" not in src
    assert "venue.has_credentials()" in src
    assert "venue.key_variable" in src


# ---------------------------------------------------------------------------
# HIGH — one plan cannot become two submissions
# ---------------------------------------------------------------------------

def test_the_idempotency_basis_is_claimed_before_the_first_await():
    """`_handle` is not serialised — the trigger path and the session loop can
    both reach it — and every `await` is a yield point. The check and the
    `_submitted[basis] = ...` writes were separated by the venue-rules fetch, the
    quantisation and the TAR publish, so two tasks carrying the SAME plan both
    passed the duplicate test before either recorded it. For an open that means
    the position is opened twice, at full size, on one decision.

    A CLOSE IS EXEMPT ON PURPOSE: `_close` withholds its basis on a wiring
    failure precisely so the exit stays retryable (invariant 4), and claiming it
    here would refuse that retry while the position stayed open.
    """
    from backend.services.execution_service import ExecutionService

    src = inspect.getsource(ExecutionService._handle)
    # Matched as a STATEMENT — the line, with its indentation — not as a
    # substring. The comment above the claim quotes the same expression to
    # explain what used to happen, and anchoring on a bare substring found the
    # explanation instead of the code. This project has made that mistake
    # before; a test that matches a docstring is a test of the docstring.
    claim = src.index("\n            _submitted[basis] = receipt")
    # The first real yield point, skipping comments — the block above the claim
    # describes the awaits that used to sit between the check and the set.
    offset = 0
    first_await = None
    for line in src.splitlines(keepends=True):
        if "await " in line and not line.lstrip().startswith("#"):
            first_await = offset
            break
        offset += len(line)
    assert first_await is not None
    assert claim < first_await, (
        "the basis must be claimed before anything yields to the event loop"
    )
    assert 'if event.intent != "close":' in src[:claim]


def test_two_concurrent_identical_plans_produce_one_submission():
    """Driven, not read. The failure mode only exists under real concurrency, so
    a sequential test would pass against the broken version too."""
    import backend.services.execution_service as es

    es.reset_for_tests()
    svc = es.ExecutionService()

    class _Plan:
        idempotency_basis = "same-basis"
        symbol = "XRP/USDT"
        intent = "open"

    async def _both():
        return await asyncio.gather(
            svc._handle(_Plan()), svc._handle(_Plan()), return_exceptions=True,
        )

    receipts = asyncio.run(_both())
    duplicates = [r for r in receipts if getattr(r, "outcome", None) == "duplicate"]
    assert len(duplicates) == 1, (
        f"exactly one of two identical concurrent plans must be refused: {receipts}"
    )
