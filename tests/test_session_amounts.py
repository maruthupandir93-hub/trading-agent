"""A session's start and target are ACCOUNT amounts, and where each comes from.

THE DISTINCTION THIS FILE EXISTS TO PIN
---------------------------------------
"$2 into $5" is a statement about the wallet, not about the price of a coin. The
session ends when the ACCOUNT reaches the target, whatever the instrument happens
to be worth — a price target would say nothing about how much was staked, so the
same move could double the account or barely touch it.

And the two books get the starting figure from different places, deliberately:

    paper   the operator types it, and it is WRITTEN to the paper book's cash so
            the run is genuinely sized and scored at that size
    real    it is the exchange's own balance and cannot be typed at all

The second half matters more than it looks. A typed real starting figure would be
the denominator of every percentage the session reports while the venue held a
different number.
"""

from __future__ import annotations

import pytest

from backend.services import trading_session as sessions


@pytest.fixture
def paper_book(monkeypatch):
    """An in-memory paper book, so a test never writes the operator's real one."""
    book = {"paper": {"cash": 10_000.0, "positions": []}, "real": {"positions": []}}

    async def get_portfolio():
        import copy

        return copy.deepcopy(book)

    async def update_portfolio(updates):
        book.clear()
        book.update(updates)
        return book

    import backend.services.portfolio_store as store

    monkeypatch.setattr(store, "get_portfolio", get_portfolio)
    monkeypatch.setattr(store, "update_portfolio", update_portfolio)
    return book


# ---------------------------------------------------------------------------
# The paper starting amount
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_paper_starting_amount_is_written_to_the_book(paper_book):
    """Not merely recorded on the session — WRITTEN.

    Recording 2.00 on the session while the book still held 10,000 would leave
    every downstream number about the 10,000: the Risk Gateway would size a
    percentage of ten thousand, one position would exceed the entire notional
    stake, and the progress bar would not visibly move. The run would look like a
    $2 experiment and behave like a $10,000 one.
    """
    await sessions.set_paper_starting_amount(2.0)
    assert paper_book["paper"]["cash"] == 2.0


@pytest.mark.asyncio
async def test_setting_the_starting_amount_is_refused_while_positions_are_open(paper_book):
    """Rewriting cash underneath a position leaves an incoherent book.

    Equity would be part old-basis and part new, and the P&L on the next close
    would be measured against capital that never funded it.
    """
    paper_book["paper"]["positions"] = [{"symbol": "SOL/USDT", "qty": 1.0, "avgCost": 100.0}]

    with pytest.raises(ValueError, match="positions are open"):
        await sessions.set_paper_starting_amount(2.0)

    # And the book is untouched — a refusal that had already written would be worse
    # than no refusal at all.
    assert paper_book["paper"]["cash"] == 10_000.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0.0, -5.0])
async def test_a_non_positive_starting_amount_is_refused(paper_book, bad):
    with pytest.raises(ValueError):
        await sessions.set_paper_starting_amount(bad)


@pytest.mark.asyncio
async def test_a_real_session_cannot_be_given_a_starting_amount(monkeypatch, paper_book):
    """The real starting figure is the exchange's, and typing one is refused."""
    monkeypatch.setattr(sessions, "_tab_for_session", lambda: "real")

    with pytest.raises(ValueError, match="cannot be set for a REAL session"):
        await sessions.start_session(
            symbol="SOL/USDT", leverage=2, target_equity=5.0, start_amount=2.0,
        )


# ---------------------------------------------------------------------------
# The real balance
# ---------------------------------------------------------------------------

def _reset_balance_cache():
    sessions._real_balance_cache.update({"value": None, "at": 0.0, "error": None})


@pytest.fixture(autouse=True)
def _clean_balance_cache():
    _reset_balance_cache()
    yield
    _reset_balance_cache()


class _FakeClient:
    def __init__(self, payload, *, count):
        self._payload = payload
        self.calls = count

    async def fetch_balance(self):
        self.calls[0] += 1
        return self._payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"USDT": {"free": 12.5}},
        {"free": {"USDT": 12.5}},
    ],
)
async def test_the_free_usdt_balance_is_read_from_either_ccxt_shape(monkeypatch, payload):
    """Both shapes are real; which appears depends on the venue and market type."""
    calls = [0]
    import backend.services.exchange_client as ec

    monkeypatch.setattr(ec, "get_exchange_client", lambda: _FakeClient(payload, count=calls))
    assert await sessions.real_account_balance() == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_the_balance_is_cached_because_the_panel_polls_every_five_seconds(monkeypatch):
    """`/api/session` is polled at 5s. Reading the venue each time is a private,
    weight-bearing call for a figure that only moves on a fill."""
    calls = [0]
    import backend.services.exchange_client as ec

    monkeypatch.setattr(
        ec, "get_exchange_client", lambda: _FakeClient({"USDT": {"free": 7.0}}, count=calls)
    )

    for _ in range(5):
        assert await sessions.real_account_balance() == pytest.approx(7.0)
    assert calls[0] == 1

    # ...and `force` still reaches the venue, for the one place that must.
    await sessions.real_account_balance(force=True)
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_an_unreadable_balance_is_None_and_never_zero(monkeypatch):
    """A zero balance and an unreadable one are different facts, and only one of
    them means the operator has no money."""
    import backend.services.exchange_client as ec

    class _Broken:
        async def fetch_balance(self):
            raise RuntimeError("auth failed")

    monkeypatch.setattr(ec, "get_exchange_client", lambda: _Broken())

    assert await sessions.real_account_balance() is None
    assert sessions.real_balance_error() is not None


@pytest.mark.asyncio
async def test_real_equity_falls_back_to_the_exchange_balance(monkeypatch, paper_book):
    """The local store has never held a real cash figure — it tracks what the agent
    did, not what the venue says the account holds. Without this fallback
    `current_equity('real')` was None forever and a real session was unstartable.
    """
    import backend.services.exchange_client as ec

    calls = [0]
    monkeypatch.setattr(
        ec, "get_exchange_client", lambda: _FakeClient({"USDT": {"free": 42.0}}, count=calls)
    )

    assert await sessions.current_equity("real") == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_the_target_is_never_passed_to_sizing():
    """CLAUDE.md's hardest constraint on this file, asserted structurally.

    `target_equity` must appear only in the stop check. A session that sized up
    because it was behind would push harder the further behind it fell, which is a
    martingale — and the further it fell the harder it would push.
    """
    import inspect

    source = inspect.getsource(sessions)
    # Every mention, with its surrounding line, so a new one has to be justified.
    lines = [ln.strip() for ln in source.splitlines() if "target_equity" in ln and not ln.strip().startswith("#")]

    forbidden = ("size", "qty", "risk_per_trade", "leverage", "notional", "position_size")
    offenders = [ln for ln in lines if any(word in ln.lower() for word in forbidden)]
    assert not offenders, f"target_equity reached sizing: {offenders}"
