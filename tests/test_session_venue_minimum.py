"""A session may not be started on a coin the account is too small to open.

THE MEASUREMENT THAT PROMPTED THIS. Choosing a rotation set for the operator's
live $1.92 account on 2026-10-09. The last column is the smallest order the
venue will accept -- whichever of the two minimums binds:

    XRP   minQty 0.1   = $0.14    min notional  $5    -> $5.00
    SOL   minQty 0.01  = $1.10    min notional  $5    -> $5.00
    DOGE  minQty 1     = $0.08    min notional  $5    -> $5.00
    ADA   minQty 1     = $0.24    min notional  $5    -> $5.00
    SUI   minQty 0.1   = $0.11    min notional  $5    -> $5.00
    BNB   minQty 0.01  = $7.39    min notional  $5    -> $7.39
    AVAX  minQty 1     = $10.17   min notional  $5    -> $10.17
    ETH   minQty 0.001 = $2.49    min notional $20    -> $20.00   NEVER

The whole account at 100% and 10x is $19.18 of notional, so ETH can NEVER be
opened at this size -- and `ETH/USDT` is the very example this project's
untradeable refusal suggests when it turns a symbol down. An ETH slot in a
five-coin rotation is a fifth of every cycle spent reaching a refusal at the
last step: the same waste as the 55 doomed BTC runs, and harder to see, because
ETH IS a tradeable instrument and nothing upstream of the venue objects to it.

A FIRST PASS OF THIS TABLE ALSO LISTED LTC AND LINK AT $20, from a hand-rolled
read of `exchangeInfo`. Both are $5. The numbers here come from ccxt's parsed
limits cross-checked against the raw Binance filters in the same market object,
which is also the pair `check_size` itself reads -- a table measured one way and
enforced another is how a pre-check starts disagreeing with the gate.

WHAT THESE TESTS GUARD is not the arithmetic -- it is the three decisions:

  1. the test is made at the LARGEST size the session can reach, so a NO is a
     statement about the session rather than about one moment's volatility
  2. an unreadable venue is NOT a refusal, or an outage stops an operator
     starting a session over a check that only ever saves wasted analysis
  3. a rotated symbol is REFUSED, never silently dropped
"""

from __future__ import annotations

import inspect

import pytest

from backend.services import trading_session as ts
from backend.services.venue import SizingCheck
from tests.sourceutil import code_only


class _FakeVenue:
    """Enough of `Venue` for the pre-check, with the venue's answer pinned."""

    def __init__(self, *, check, price=2487.36, resolved="ETH/USDT:USDT"):
        self.id = "binance"
        self._check = check
        self._price = price
        self._resolved = resolved
        self.sizes_asked = []
        self.public = self

    async def resolve_symbol(self, symbol):
        return self._resolved

    async def fetch_ticker(self, resolved):
        return {"last": self._price}

    async def check_size(self, symbol, qty, price):
        self.sizes_asked.append((symbol, qty, price))
        return self._check


@pytest.fixture
def _no_cached_price(monkeypatch):
    """Force the venue-ticker path: the cache is cold for exactly the symbols a
    new rotation adds, which is the case this check exists for."""
    import backend.services.market_data as md

    monkeypatch.setattr(md, "get_price", lambda symbol: 0.0)


# ---------------------------------------------------------------------------
# 1. The test is made at the largest size the session can reach
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_it_asks_the_venue_about_the_biggest_order_the_session_could_place(
    monkeypatch, _no_cached_price
):
    """If the LARGEST order is refused, every smaller one is too -- so one call
    settles it for the whole session. Asking about some mid-sized guess would
    make the answer depend on the moment it happened to be asked."""
    venue = _FakeVenue(check=SizingCheck(ok=True, qty=0.007), price=2500.0)
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)

    out = await ts._venue_minimum_refusal(
        "ETH/USDT", equity=1.9176, leverage=10, capital_fraction=1.0
    )
    assert out is None

    _, qty, price = venue.sizes_asked[0]
    assert price == 2500.0
    # 100% of $1.9176 at 10x = $19.176 of notional.
    assert qty == pytest.approx(19.176 / 2500.0, rel=1e-6)


@pytest.mark.asyncio
async def test_a_smaller_allocation_asks_about_a_smaller_order(monkeypatch, _no_cached_price):
    """The ceiling is the session's own allocation and leverage, not the whole
    account -- a 25% session genuinely cannot reach what a 100% one can."""
    venue = _FakeVenue(check=SizingCheck(ok=True, qty=1.0), price=100.0)
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)

    await ts._venue_minimum_refusal("SOL/USDT", equity=100.0, leverage=3, capital_fraction=0.25)
    _, qty, _ = venue.sizes_asked[0]
    assert qty == pytest.approx(0.25 * 100.0 * 3 / 100.0)


@pytest.mark.asyncio
async def test_a_refusal_names_both_numbers(monkeypatch, _no_cached_price):
    """"ETH is too small" is unactionable. The operator needs the size they can
    reach AND the size the venue demands, or they cannot tell whether to add
    funds, raise the leverage, or pick a different coin."""
    venue = _FakeVenue(
        check=SizingCheck(
            ok=False, qty=0.007, min_qty=0.001, min_notional=20.0,
            reason="notional 17.41 is below ETH/USDT's minimum of 20.00 on binance",
        ),
        price=2487.36,
    )
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)

    out = await ts._venue_minimum_refusal(
        "ETH/USDT", equity=1.9176, leverage=10, capital_fraction=1.0
    )
    assert out is not None
    assert "ETH/USDT" in out
    assert "19.18" in out, "the reachable size must be stated"
    assert "20.00" in out, "the venue's minimum must be stated"


@pytest.mark.asyncio
async def test_a_min_quantity_that_costs_more_than_the_minimum_notional_wins(
    monkeypatch, _no_cached_price
):
    """AVAX is the live example: MIN_NOTIONAL is $5 but one contract costs
    $10.17, so $5 is not the binding number. Reporting the smaller of the two
    would send the operator to top up to a figure that still cannot trade."""
    venue = _FakeVenue(
        check=SizingCheck(
            ok=False, qty=1.0, min_qty=1.0, min_notional=5.0,
            reason="0 is below AVAX/USDT's minimum quantity of 1 on binance",
        ),
        price=10.17,
        resolved="AVAX/USDT:USDT",
    )
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)

    out = await ts._venue_minimum_refusal(
        "AVAX/USDT", equity=0.5, leverage=10, capital_fraction=1.0
    )
    assert out is not None and "10.17" in out


# ---------------------------------------------------------------------------
# 2. An unreadable venue is not a refusal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_venue_that_cannot_be_asked_does_not_block_the_session(
    monkeypatch, _no_cached_price
):
    """THIS IS THE PROPERTY THAT KEEPS THE CHECK SAFE TO ADD.

    It saves wasted analysis; it is not a safety control. The entry path still
    enforces the venue's minimums, so a transient outage must cost the operator
    nothing. Refusing here would turn a network blip into "you cannot start a
    session", which is strictly worse than the waste it prevents.
    """
    class _Broken(_FakeVenue):
        async def resolve_symbol(self, symbol):
            raise RuntimeError("venue unreachable")

    monkeypatch.setattr(
        "backend.services.venue.get_venue",
        lambda: _Broken(check=SizingCheck(ok=False, qty=0.0)),
    )
    assert await ts._venue_minimum_refusal(
        "ETH/USDT", equity=1.0, leverage=10, capital_fraction=1.0
    ) is None


@pytest.mark.asyncio
async def test_a_slow_venue_is_not_a_refusal_either(monkeypatch, _no_cached_price):
    """IT HUNG THE TEST SUITE BEFORE IT WAS DEADLINED, which is the same thing
    it would have done to the operator's HTTP request.

    `start_session` is called from a route and this is the only I/O it does. A
    check whose entire value is saving wasted analysis must never become the
    slowest thing in the path it guards -- the `external_consultation` lesson,
    where a node nothing waits on was holding a 30s decision loop for five
    minutes. Unreadable and slow get the same answer: not a refusal.
    """
    import asyncio

    class _Slow(_FakeVenue):
        async def resolve_symbol(self, symbol):
            await asyncio.sleep(30)
            return "ETH/USDT:USDT"

    monkeypatch.setattr(ts, "_VENUE_PRECHECK_DEADLINE_S", 0.05)
    monkeypatch.setattr(
        "backend.services.venue.get_venue",
        lambda: _Slow(check=SizingCheck(ok=False, qty=0.0)),
    )

    started = asyncio.get_event_loop().time()
    out = await ts._venue_minimum_refusal(
        "ETH/USDT", equity=1.0, leverage=10, capital_fraction=1.0
    )
    assert out is None
    assert asyncio.get_event_loop().time() - started < 5.0, "the deadline did not bound it"


@pytest.mark.asyncio
async def test_no_price_anywhere_is_not_a_refusal(monkeypatch, _no_cached_price):
    """Invariant 6. Without a price the comparison cannot be made, and a verdict
    that was not measured must not be stated."""
    venue = _FakeVenue(check=SizingCheck(ok=False, qty=0.0), price=0.0)
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)
    assert await ts._venue_minimum_refusal(
        "ETH/USDT", equity=1.0, leverage=10, capital_fraction=1.0
    ) is None


@pytest.mark.asyncio
async def test_an_unresolvable_symbol_is_not_a_refusal(monkeypatch, _no_cached_price):
    venue = _FakeVenue(check=SizingCheck(ok=False, qty=0.0), resolved=None)
    monkeypatch.setattr("backend.services.venue.get_venue", lambda: venue)
    assert await ts._venue_minimum_refusal(
        "WAT/USDT", equity=1.0, leverage=10, capital_fraction=1.0
    ) is None


# ---------------------------------------------------------------------------
# 3. Every scanned symbol is checked, and start_session raises rather than drops
# ---------------------------------------------------------------------------

def test_start_session_checks_the_primary_symbol_and_every_rotated_one():
    """Asserted against the source because driving `start_session` needs a live
    book, an equity read and a venue -- and the property that matters is
    structural: the loop covers `[symbol] + extra`, not just one of them. A
    rotated coin that is never checked is the silent drop this refusal exists
    to prevent."""
    src = inspect.getsource(ts.start_session)
    assert "_venue_minimum_refusal" in src
    assert "[symbol.strip().upper()] + extra" in src, (
        "the primary symbol and every rotated one must both be checked"
    )
    # It must RAISE, which is what distinguishes refusing from dropping.
    idx = src.index("_venue_minimum_refusal")
    assert "raise ValueError" in src[idx:idx + 500]


def test_the_check_runs_after_equity_leverage_and_the_fraction_are_settled():
    """The ceiling is `fraction x equity x leverage`. Running the check before
    any of the three were validated would test a number the session will not
    use -- and `cf` in particular is CLAMPED, so the raw argument is the wrong
    input."""
    src = inspect.getsource(ts.start_session)
    assert src.index("equity = await current_equity") < src.index("_venue_minimum_refusal")
    assert src.index("cf = 1.0") < src.index("_venue_minimum_refusal")


def test_it_reuses_the_venues_own_sizing_check():
    """NOT a second copy of the minimums. `check_size` is the exact call that
    refuses at execution time; re-deriving the rules here is how
    `lib/riskManager.ts` and `core/risk_manager.py` drifted apart on the ATR
    multipliers, and a pre-check that disagrees with the real gate is worse
    than no pre-check at all."""
    src = inspect.getsource(ts._venue_minimum_refusal_inner)
    assert "venue.check_size(" in src

    # COMMENTS AND DOCSTRINGS STRIPPED, the same lesson as
    # `test_binance_testnet_mirror`'s one-host assertion: the explanation has to
    # keep naming the venue rules it says not to re-derive, and a mention is not
    # an implementation. This failed on its own docstring first.
    assert "MIN_NOTIONAL" not in code_only(src)
    assert "amount_to_precision" not in code_only(src)
    assert "stepSize" not in code_only(src)
