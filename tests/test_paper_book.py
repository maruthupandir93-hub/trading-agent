"""The signed paper book — the write the agent never made, and the maths behind it.

WHY THIS FILE EXISTS
====================
`execution_agent` wrote a row to `trades`, handed the position to the monitor, and
NEVER TOUCHED THE BOOK. On the paper account that produced four separate "broken
panels" from one absent write:

    cash sat at its starting figure forever, however many trades filled
    `positions` stayed empty, so "Open positions" read 0
    there was no unrealized P&L to move when price moved
    `current_equity()` is cash + marked positions, so session progress never moved

And it could not simply call `buy_paper`, which is long-only: this agent SHORTS,
and `sell_paper` rejects a sell with no long open while computing P&L as
(exit - entry) — the opposite sign for a short.

THE TWO ARITHMETIC CLAIMS PINNED HERE
=====================================
  1. A SHORT'S P&L IS SIGNED THE OTHER WAY. Getting this wrong reports a losing
     short as a winner, and the risk layer would add to it.
  2. EQUITY IS FREE CASH + LOCKED MARGIN + UNREALIZED, not `cash + qty*price`.
     The old formula is only correct at 1x; at 10x it invented $6,300 of equity
     on a $700 margin position, and every percentage derived from it was wrong in
     the direction that flatters the account.
"""

from __future__ import annotations

import pytest

from backend.services import portfolio_store
from backend.services.portfolio_store import apply_paper_fill, book_equity, position_pnl


@pytest.fixture(autouse=True)
def book(monkeypatch):
    """A fresh in-memory book with no database behind it, and NO trading fee.

    THE FEE IS ZEROED HERE ON PURPOSE, and it is not a fixture being kinder than
    the venue. Every test in this file asserts MARGIN AND DIRECTION mechanics —
    that margin is locked and released proportionally, that a short profits when
    price falls, that capital in one position is unavailable to another. A fee
    shifts every one of those figures by a constant that has nothing to do with
    the property under test, and folding it into each expected number would make
    the arithmetic unreadable without testing anything more.

    Fees are charged by this book and are covered explicitly at the bottom of
    this file, and exhaustively in `tests/test_fees.py`.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", "0")
    state = {"paper": {"cash": 10_000.0, "positions": []}, "real": {"positions": []}}
    monkeypatch.setattr(portfolio_store, "_portfolio", state)

    async def no_persist():
        return False

    monkeypatch.setattr(portfolio_store, "_persist", no_persist)
    return state["paper"]


# ---------------------------------------------------------------------------
# Unrealized P&L — the signed one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "side,mark,expected",
    [
        ("buy", 110.0, 10.0),    # long, price up   -> profit
        ("buy", 90.0, -10.0),    # long, price down -> loss
        ("sell", 90.0, 10.0),    # SHORT, price down -> PROFIT
        ("sell", 110.0, -10.0),  # SHORT, price up   -> LOSS
    ],
)
def test_unrealized_pnl_is_signed_by_direction(side, mark, expected):
    pos = {"symbol": "T/USDT", "qty": 1.0, "avgCost": 100.0, "side": side}
    assert position_pnl(pos, mark) == pytest.approx(expected)


def test_an_unpriceable_position_yields_None_not_zero():
    # "We could not value this" and "this is exactly flat" are different facts,
    # and only one of them belongs in an equity total.
    pos = {"symbol": "T/USDT", "qty": 1.0, "avgCost": 100.0, "side": "buy"}
    assert position_pnl(pos, 0.0) is None


def test_a_position_with_no_side_reads_as_a_long():
    # Rows written before the column existed were longs; defaulting to None would
    # make them unvaluable and blank the equity of an untouched book.
    assert position_pnl({"qty": 1.0, "avgCost": 100.0}, 110.0) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------

def test_equity_is_correct_at_10x_where_the_old_formula_was_not():
    """`cash + qty*price` reported 31,300 for a 25,000 account.

    `buy_paper` deducts MARGIN from cash, so cash is free cash. Adding the full
    notional back double-counts the leveraged part.
    """
    book = {
        "cash": 24_300.0,
        "positions": [{
            "symbol": "BTC/USDT", "qty": 0.1, "avgCost": 70_000.0,
            "marginLocked": 700.0, "side": "buy",
        }],
    }
    flat = book_equity(book, {"BTC/USDT": 70_000.0})
    assert flat["equity"] == pytest.approx(25_000.0)

    # ...and it MOVES with the mark, which is the behaviour that was missing.
    up = book_equity(book, {"BTC/USDT": 71_000.0})
    assert up["equity"] == pytest.approx(25_100.0)
    assert up["unrealized"] == pytest.approx(100.0)


def test_equity_is_None_when_a_position_cannot_be_priced():
    book = {
        "cash": 1_000.0,
        "positions": [{"symbol": "X/USDT", "qty": 1.0, "avgCost": 10.0, "side": "buy"}],
    }
    result = book_equity(book, {})
    assert result["equity"] is None
    assert result["complete"] is False
    assert result["unpricedSymbols"] == ["X/USDT"]


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_entry_locks_margin_and_records_the_position(book):
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=0.1, price=70_000.0, leverage=10.0,
    )
    assert result["ok"] is True
    # 7,000 notional at 10x = 700 margin.
    assert book["cash"] == pytest.approx(9_300.0)
    (pos,) = book["positions"]
    assert pos["side"] == "buy"
    assert pos["marginLocked"] == pytest.approx(700.0)


@pytest.mark.asyncio
async def test_a_SHORT_entry_is_recorded_as_a_short(book):
    # `buy_paper`/`sell_paper` cannot express this at all, which is why the agent
    # could not use them.
    result = await apply_paper_fill(symbol="BTC/USDT", side="sell", qty=0.1, price=70_000.0)
    assert result["ok"] is True
    assert book["positions"][0]["side"] == "sell"


@pytest.mark.asyncio
async def test_closing_a_SHORT_at_a_lower_price_is_a_PROFIT(book):
    """The sign that the long-only path would have got backwards."""
    await apply_paper_fill(symbol="BTC/USDT", side="sell", qty=1.0, price=100.0, leverage=1.0)
    cash_after_open = book["cash"]

    result = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=1.0, price=90.0, reduce_only=True,
    )

    assert result["realized"] == pytest.approx(10.0)
    assert book["positions"] == []
    # Margin back plus the gain.
    assert book["cash"] == pytest.approx(cash_after_open + 100.0 + 10.0)


@pytest.mark.asyncio
async def test_closing_a_long_at_a_lower_price_is_a_LOSS(book):
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=1.0, price=100.0, leverage=1.0)
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="sell", qty=1.0, price=90.0, reduce_only=True,
    )
    assert result["realized"] == pytest.approx(-10.0)
    # Started at 10,000, lost 10.
    assert book["cash"] == pytest.approx(9_990.0)


@pytest.mark.asyncio
async def test_a_full_round_trip_returns_cash_to_its_starting_figure(book):
    # The property that was missing entirely: cash MOVES, and by the P&L.
    start = book["cash"]
    await apply_paper_fill(symbol="ETH/USDT", side="buy", qty=2.0, price=3_000.0, leverage=5.0)
    assert book["cash"] < start
    await apply_paper_fill(
        symbol="ETH/USDT", side="sell", qty=2.0, price=3_000.0, reduce_only=True,
    )
    assert book["cash"] == pytest.approx(start)


@pytest.mark.asyncio
async def test_a_partial_close_releases_margin_proportionally(book):
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=2.0, price=100.0, leverage=2.0)
    # 200 notional at 2x = 100 margin.
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="sell", qty=1.0, price=110.0, reduce_only=True,
    )
    assert result["action"] == "reduce"
    assert result["realized"] == pytest.approx(10.0)
    (pos,) = book["positions"]
    assert pos["qty"] == pytest.approx(1.0)
    assert pos["marginLocked"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_adding_to_a_position_averages_the_cost(book):
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=1.0, price=100.0, leverage=1.0)
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=1.0, price=120.0, leverage=1.0)
    (pos,) = book["positions"]
    assert pos["qty"] == pytest.approx(2.0)
    assert pos["avgCost"] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_a_reduce_with_nothing_open_is_REFUSED_not_reversed(book):
    """`reduceOnly` means the same thing here as at a venue.

    Absorbing it by opening a reversed position is exactly what the flag exists
    to prevent, and it would leave the book holding a position nobody asked for.
    """
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="sell", qty=1.0, price=100.0, reduce_only=True,
    )
    assert result["ok"] is False
    assert "nothing open" in result["reason"]
    assert book["positions"] == []
    assert book["cash"] == pytest.approx(10_000.0)


@pytest.mark.asyncio
async def test_an_oversized_close_clamps_and_REPORTS_the_remainder(book):
    # Silently clamping would hide that the monitor and the book disagree on size.
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=1.0, price=100.0, leverage=1.0)
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="sell", qty=3.0, price=110.0, reduce_only=True,
    )
    assert result["closedQty"] == pytest.approx(1.0)
    assert result["unmatchedQty"] == pytest.approx(2.0)
    assert book["positions"] == []


@pytest.mark.asyncio
async def test_an_opposite_side_entry_reduces_rather_than_double_booking(book):
    """One symbol carries one aggregated position in this book.

    Treating an un-flagged opposite fill as a second position would double-count
    its margin and leave two rows the equity total adds together.
    """
    await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=1.0, price=100.0, leverage=1.0)
    result = await apply_paper_fill(symbol="BTC/USDT", side="sell", qty=1.0, price=110.0)
    assert result["ok"] is True
    assert result["realized"] == pytest.approx(10.0)
    assert book["positions"] == []


@pytest.mark.asyncio
async def test_a_zero_or_negative_fill_is_refused(book):
    for qty, price in ((0.0, 100.0), (1.0, 0.0)):
        result = await apply_paper_fill(symbol="BTC/USDT", side="buy", qty=qty, price=price)
        assert result["ok"] is False
    assert book["positions"] == []


# ---------------------------------------------------------------------------
# Free cash — the operator's question: "if one trade takes the whole amount,
# does the agent open another with no money?"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_fill_that_exceeds_free_cash_is_REFUSED(book):
    """It used to succeed and drive cash NEGATIVE.

    `buy_paper` (the operator's manual path) has always refused; this path, which
    every AGENT fill takes, did not. Negative free cash understates equity, which
    understates the next position's size, which makes every later risk figure
    wrong in a compounding way — and it models leverage the venue never granted.
    """
    result = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=1.0, price=70_000.0, leverage=1.0,
    )
    assert result["ok"] is False
    assert "insufficient free cash" in result["reason"]
    assert book["cash"] == pytest.approx(10_000.0)
    assert book["positions"] == []


@pytest.mark.asyncio
async def test_capital_locked_in_one_position_is_not_available_for_another(book):
    """Two concurrent positions cannot both spend the same money.

    The first takes 6,000 of margin; the second needs 6,000 and only 4,000 is
    free, so it is refused rather than opened on credit.
    """
    first = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=6.0, price=1_000.0, leverage=1.0,
    )
    assert first["ok"] is True
    assert book["cash"] == pytest.approx(4_000.0)

    second = await apply_paper_fill(
        symbol="ETH/USDT", side="buy", qty=6.0, price=1_000.0, leverage=1.0,
    )
    assert second["ok"] is False
    assert second["freeCash"] == pytest.approx(4_000.0)
    # The first position is untouched — a refusal must not disturb what is open.
    assert len(book["positions"]) == 1


@pytest.mark.asyncio
async def test_leverage_is_what_decides_whether_it_fits(book):
    """The same notional fits at 10x and does not at 1x.

    Margin, not notional, is what leaves the account — sizing that checked
    notional against cash would refuse every leveraged trade.
    """
    at_1x = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=50.0, price=1_000.0, leverage=1.0,
    )
    assert at_1x["ok"] is False  # 50,000 notional, 50,000 margin

    at_10x = await apply_paper_fill(
        symbol="BTC/USDT", side="buy", qty=50.0, price=1_000.0, leverage=10.0,
    )
    assert at_10x["ok"] is True   # 50,000 notional, 5,000 margin
    assert book["cash"] == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# Fees — charged where they are actually paid
# ---------------------------------------------------------------------------
#
# The book deducted margin and never the commission, so a book that had traded a
# hundred times looked identical to one that had never traded. Margin is LOCKED
# and comes back on the close; a fee is SPENT and does not.
#
# These opt back IN to the fee the fixture above zeroes.


@pytest.mark.asyncio
async def test_an_entry_charges_its_fee_to_cash(book, monkeypatch):
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0005")
    result = await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="buy", qty=10.0, price=100.0, leverage=5,
    )
    assert result["ok"] is True
    # 1,000 notional at 5x = 200 margin, plus 1,000 * 0.05% = 0.50 fee.
    assert result["fee"] == pytest.approx(0.5)
    assert book["cash"] == pytest.approx(10_000.0 - 200.0 - 0.5)


@pytest.mark.asyncio
async def test_a_round_trip_at_the_same_price_LOSES_the_fees(book, monkeypatch):
    """The headline consequence, and the one the system was blind to.

    Flat price used to return cash exactly to its starting figure. In reality a
    round trip that goes nowhere costs both legs' commission — which is why a
    strategy with a thin positive gross edge can be a reliable net loser.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0005")
    start = book["cash"]
    await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="buy", qty=10.0, price=100.0, leverage=5,
    )
    result = await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="sell", qty=10.0, price=100.0, leverage=5,
        reduce_only=True,
    )
    assert result["grossRealized"] == pytest.approx(0.0)
    assert result["realized"] == pytest.approx(-0.5)      # the exit leg
    assert book["cash"] == pytest.approx(start - 1.0)     # both legs
    assert book["cash"] < start


@pytest.mark.asyncio
async def test_the_entry_fee_is_not_charged_twice(book, monkeypatch):
    """Each leg is accounted where it is PAID, so the close nets only its own.

    `position_monitor` reports a round trip's P&L net of both legs because a
    trade ROW must state the whole trip. This book instead moves cash at each
    event. Conflating the two would double-charge the entry.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0005")
    await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="buy", qty=10.0, price=100.0, leverage=5,
    )
    result = await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="sell", qty=10.0, price=110.0, leverage=5,
        reduce_only=True,
    )
    # Gross +100; the close subtracts ONLY its own leg (1,100 * 0.05% = 0.55).
    assert result["grossRealized"] == pytest.approx(100.0)
    assert result["realized"] == pytest.approx(100.0 - 0.55)


@pytest.mark.asyncio
async def test_an_entry_that_is_affordable_only_before_fees_is_REFUSED(book, monkeypatch):
    """Cash must never go negative, and the fee is part of what an entry costs.

    Checking `margin > free_cash` and then subtracting `margin + fee` lets a
    position sized to exactly the available cash pass the check and push the
    balance negative. That is not cosmetic: `book_equity` is free cash + locked
    margin + unrealized, so a negative first term understates equity, which
    understates the next position's size, and every later risk calculation is
    wrong in a compounding way.

    Reachable rather than theoretical under broker-style sizing: at 100% capital
    allocation the Risk Gateway deliberately sizes to the whole pool, so
    "exactly affordable before fees" is the normal case at the top of the range.
    """
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0005")
    book["cash"] = 200.0

    # 1,000 notional at 5x = exactly 200.00 margin, plus a 0.50 fee.
    result = await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="buy", qty=10.0, price=100.0, leverage=5,
    )
    assert result["ok"] is False
    assert result["fee"] == pytest.approx(0.5)
    assert book["cash"] == pytest.approx(200.0), "a refused entry must not move cash"


@pytest.mark.asyncio
async def test_an_entry_that_clears_margin_AND_fee_is_accepted(book, monkeypatch):
    """The other side of the bound — the check must not be so strict it refuses a
    position the account can actually afford."""
    monkeypatch.setenv("FEE_TAKER_RATE", "0.0005")
    book["cash"] = 200.5

    result = await portfolio_store.apply_paper_fill(
        symbol="SOL/USDT", side="buy", qty=10.0, price=100.0, leverage=5,
    )
    assert result["ok"] is True
    assert book["cash"] == pytest.approx(0.0)
    assert book["cash"] >= 0.0
