"""Reconciliation: what the venue holds vs what this system thinks it holds.

THE COMPARISON IS PURE AND IS TESTED THAT WAY. `_compare` takes both books and
returns the discrepancies, so the severity rules — which decide what wakes an
operator — are checked without a network or an exchange account. That is the only
way they get checked at all.

THE MOST IMPORTANT TEST IN THIS FILE is that a venue read of `None` is never
treated as an empty book. `None` means "we could not ask". Reporting it as "the
venue holds nothing" would flag every real position as a phantom on a single
network blip — and if anything ever acted on that report, a timeout would flatten
the book.
"""

from __future__ import annotations

import pytest

from backend.services import reconciliation
from backend.services.reconciliation import _compare, reconcile


def local(symbol="BTC/USDT", qty=1.0, side="buy", tab="real"):
    return {"symbol": symbol, "qty": qty, "side": side, "tab": tab, "entry_price": 70_000.0}


def at_venue(symbol="BTC/USDT", qty=1.0, side="long"):
    return {"symbol": symbol, "qty": qty, "side": side, "entryPrice": 70_000.0}


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def test_two_agreeing_books_produce_no_discrepancies():
    assert _compare([local()], [at_venue()]) == []


def test_a_position_the_venue_does_not_have_is_CRITICAL():
    """The monitor is guarding something that is not there.

    Its next stop touch sends a close for a position that does not exist — which,
    on a venue where the account is flat, is an order to OPEN the opposite one.
    """
    (d,) = _compare([local()], [])
    assert d.kind == "missing_at_venue"
    assert d.severity == "critical"
    assert "closed by hand" in d.detail


def test_a_position_only_the_venue_has_is_a_WARNING_not_critical():
    """The operator may legitimately be trading this account by hand.

    It is unmonitored either way, and saying so is the point — but closing or
    adopting it automatically would be acting on a guess about whose trade it is.
    """
    (d,) = _compare([], [at_venue("ETH/USDT")])
    assert d.kind == "unknown_locally"
    assert d.severity == "warning"


def test_a_size_mismatch_beyond_tolerance_is_reported():
    (d,) = _compare([local(qty=1.0)], [at_venue(qty=0.5)])
    assert d.kind == "size_mismatch"
    assert d.severity == "critical"


def test_a_size_difference_within_tolerance_is_not_a_discrepancy():
    # Fees taken in the base asset and each venue's own rounding mean sizes never
    # match to the last bit. Flagging that would make the report unreadable.
    assert _compare([local(qty=1.0)], [at_venue(qty=0.995)]) == []


def test_a_side_mismatch_is_reported_because_the_stop_is_on_the_wrong_side():
    (d,) = _compare([local(side="buy")], [at_venue(side="short")])
    assert d.kind == "side_mismatch"
    assert d.severity == "critical"
    assert "wrong side" in d.detail


def test_symbols_are_compared_independently():
    out = _compare(
        [local("BTC/USDT"), local("ETH/USDT")],
        [at_venue("BTC/USDT"), at_venue("SOL/USDT")],
    )
    kinds = {(d.symbol, d.kind) for d in out}
    assert ("ETH/USDT", "missing_at_venue") in kinds
    assert ("SOL/USDT", "unknown_locally") in kinds
    assert not any(d.symbol == "BTC/USDT" for d in out)


# ---------------------------------------------------------------------------
# The I/O wrapper
# ---------------------------------------------------------------------------

class _Venue:
    id = "binance"

    def __init__(self, positions, creds=True):
        self._positions = positions
        self._creds = creds

    def has_credentials(self):
        return self._creds

    async def open_positions(self):
        return self._positions


class _Monitor:
    def __init__(self, positions):
        self._positions = positions

    def snapshot_open(self):
        return self._positions


def _wire(monkeypatch, *, venue_positions, local_positions, creds=True):
    import backend.agents.position_monitor as pm
    import backend.services.venue as venue_mod

    monkeypatch.setattr(venue_mod, "get_venue", lambda: _Venue(venue_positions, creds))
    monkeypatch.setattr(pm, "get_position_monitor", lambda: _Monitor(local_positions))


@pytest.mark.asyncio
async def test_an_unreachable_venue_is_NOT_reported_as_an_empty_book(monkeypatch):
    """THE test in this file.

    `open_positions()` returns None when it could not ask. Collapsing that into []
    would mark every real position `missing_at_venue` on one timeout.
    """
    _wire(monkeypatch, venue_positions=None, local_positions=[local()])

    report = await reconcile()

    assert report.ok is False
    assert report.venue_positions is None
    assert report.discrepancies == []  # nothing was compared, so nothing is claimed
    assert "NOT a report that the venue holds nothing" in (report.error or "")


@pytest.mark.asyncio
async def test_a_genuinely_empty_venue_book_IS_compared(monkeypatch):
    # [] is a real answer and must produce the critical finding that None does not.
    _wire(monkeypatch, venue_positions=[], local_positions=[local()])

    report = await reconcile()

    assert report.venue_positions == 0
    assert [d.kind for d in report.discrepancies] == ["missing_at_venue"]
    assert report.ok is False


@pytest.mark.asyncio
async def test_paper_positions_are_never_reconciled(monkeypatch):
    """A paper position has no venue counterpart.

    Comparing them would report every simulated trade as a phantom, which would
    bury a real discrepancy in noise.
    """
    _wire(monkeypatch, venue_positions=[], local_positions=[local(tab="paper")])

    report = await reconcile()

    assert report.local_positions == 0
    assert report.discrepancies == []


@pytest.mark.asyncio
async def test_no_credentials_is_reported_as_not_checked_not_as_agreement(monkeypatch):
    _wire(monkeypatch, venue_positions=None, local_positions=[local()], creds=False)

    report = await reconcile()

    assert report.venue_positions is None
    assert "no venue credentials" in (report.error or "")


@pytest.mark.asyncio
async def test_agreement_reports_ok(monkeypatch):
    _wire(monkeypatch, venue_positions=[at_venue()], local_positions=[local()])

    report = await reconcile()

    assert report.ok is True
    assert report.venue_positions == 1 and report.local_positions == 1


def test_the_reconciler_never_repairs():
    """Asserted against the module's own source, because the temptation is real.

    Every automatic fix is itself a trade: forgetting a local position abandons a
    real one if the venue read was stale, and closing an unknown venue position
    sends a market order nobody asked for — possibly on the operator's own manual
    trade in the same account.
    """
    import inspect

    source = inspect.getsource(reconciliation)
    for forbidden in ("close_position", "market_order", "create_order", "_open.pop"):
        assert forbidden not in source, f"the reconciler must not call {forbidden}"
