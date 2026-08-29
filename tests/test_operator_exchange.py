"""The operator's manual exchange path — the one HTTP route that places orders.

WHAT THIS GUARDS
----------------
`api/operator_exchange.py` exists because the operator's manual trading path had
to move off Vercel: a Vercel handler runs in a Vercel-chosen region and Binance
refuses restricted regions with a 451, so a real order would have failed exactly
when real money was on the line.

Moving it created the thing `api/exchange.py` warns about by name — an HTTP
endpoint that can reach an exchange. These tests assert the properties that make
that acceptable rather than dangerous. The plane separation itself is asserted in
`tests/test_api_surface.py`; this file covers the module's own behaviour.

NOTHING HERE PLACES A REAL ORDER. Every venue call is faked. The network guard in
conftest.py would fail the test if one leaked out, which is itself part of the
point.
"""

import pytest

from backend.api import operator_exchange as oe


# ---------------------------------------------------------------------------
# Symbol handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "app_symbol,expected",
    [("BTC/USDT", "BTCUSDT"), ("eth/usdt", "ETHUSDT"), ("BTCUSDT", "BTCUSDT")],
)
def test_native_symbol_is_the_joined_upper_form(app_symbol, expected):
    """Both venues' spot APIs want 'BTCUSDT'; the app stores 'BTC/USDT'."""
    assert oe._to_exchange_symbol(app_symbol) == expected


@pytest.mark.parametrize(
    "given,expected",
    [
        ("BTC/USDT", "BTC/USDT"),
        ("BTCUSDT", "BTC/USDT"),
        ("ETHUSDC", "ETH/USDC"),
        ("ethbtc", "ETH/BTC"),
    ],
)
def test_ccxt_symbol_accepts_either_form(given, expected):
    """ccxt wants the slashed form, and BOTH inputs must work.

    The app stores 'BTC/USDT', but the Next.js route this replaced accepted the
    joined form too. Rejecting it here would break a caller that worked
    yesterday, and the failure would be a rejected ORDER rather than a 400 —
    the worst place to discover a format mismatch.
    """
    assert oe._to_ccxt_symbol(given) == expected


def test_an_unsplittable_symbol_is_passed_through_not_mangled():
    """A symbol whose quote asset is not one we know is handed to ccxt as-is.

    Guessing a split point would produce a plausible-looking pair that does not
    exist, and the order would be rejected for a reason that pointed at the
    wrong thing. ccxt's own error naming the unknown market is more useful.
    """
    assert oe._to_ccxt_symbol("WEIRDPAIR") == "WEIRDPAIR"


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------


def test_credentials_never_render_the_secret():
    """A stray log line, traceback or debugger frame must not print the secret.

    This is not hypothetical: the natural way to debug a failing order is to log
    the request, and the request carries the operator's API secret. `__repr__`
    is overridden so that doing so leaks nothing.
    """
    creds = oe.Credentials(apiKey="AKIAEXAMPLEKEY1234", apiSecret="super-secret-value", testnet=True)

    for rendered in (repr(creds), str(creds), f"{creds}"):
        assert "super-secret-value" not in rendered
        assert "AKIAEXAMPLEKEY1234" not in rendered
        # The last four are kept so two accounts are still tellable apart.
        assert "1234" in rendered


def test_the_error_describer_returns_only_type_and_message():
    """ccxt exceptions can embed the signed request that produced them.

    Returning the exception's full context to the caller would hand back a
    request signed with the operator's secret.
    """
    described = oe._describe(ValueError("bad thing happened"))
    assert described == "ValueError: bad thing happened"


# ---------------------------------------------------------------------------
# Persistence — the record that separates an operator order from an agent order
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


async def test_a_testnet_order_is_recorded_as_paper_not_real(monkeypatch):
    """Testnet fills must never mix into real history.

    The same rule `settings.execution_tab` applies to the agent's own fills. Two
    books in one table with no way to separate them afterwards is a permanent
    corruption, not a display bug.
    """
    conn = _FakeConn()
    monkeypatch.setattr(oe, "get_db_pool", lambda: _FakePool(conn))

    await oe._persist_operator_trade(
        exchange_order_id="oid-1", symbol="BTC/USDT", side="buy",
        qty=0.5, price=70_000.0, testnet=True,
    )

    assert len(conn.calls) == 1
    args = conn.calls[0][1]
    assert "paper" in args
    assert "real" not in args


async def test_a_mainnet_order_is_recorded_as_real_and_tagged_manual(monkeypatch):
    """`origin_tag='manual-click'` is what makes an operator order distinguishable
    from an agent order forever after. The route this replaced recorded nothing
    server-side at all."""
    conn = _FakeConn()
    monkeypatch.setattr(oe, "get_db_pool", lambda: _FakePool(conn))

    await oe._persist_operator_trade(
        exchange_order_id="oid-2", symbol="ETH/USDT", side="sell",
        qty=2.0, price=3_000.0, testnet=False,
    )

    args = conn.calls[0][1]
    assert "real" in args
    assert "manual-click" in args
    assert "oid-2" in args


async def test_no_row_is_written_without_a_fill_price(monkeypatch, caplog):
    """An order with no reported fill price writes NOTHING, and says so.

    THREE WRONG ANSWERS WERE AVAILABLE HERE AND ALL OF THEM LIE:

    1. Substitute a plausible price — forbidden outright (invariant 6).
    2. Write `price = 0` — `lib/tradeStore.server.ts` reads it back as a number,
       so the operator's trade log would show a real order executed at zero.
    3. Write `price = NULL` — `db/schema.sql` declares the column NOT NULL, and
       relaxing it produces exactly (2), because that file coerces NULL with
       `toNumber(r.price) ?? 0`.

    A `trades` row means "a fill happened at this price". An order the venue
    accepted but has not reported a fill for is an ORDER, not a trade. So it is
    logged at ERROR with the reconciliation step and reported as unrecorded —
    an honest gap instead of a plausible-looking lie.
    """
    conn = _FakeConn()
    monkeypatch.setattr(oe, "get_db_pool", lambda: _FakePool(conn))

    with caplog.at_level("ERROR"):
        result = await oe._persist_operator_trade(
            exchange_order_id="oid-3", symbol="BTC/USDT", side="buy",
            qty=0.1, price=None, testnet=True,
        )

    assert conn.calls == [], "no trade row may be written without a fill price"
    assert result["recorded"] is False
    assert result["note"]

    # The operator must be able to find the order and reconcile it.
    assert "oid-3" in caplog.text
    assert "order/status" in caplog.text


def test_the_trades_price_column_must_stay_not_null():
    """Pins the constraint the writer above depends on.

    If someone relaxes `trades.price` to nullable to "simplify" the writer, the
    operator's trade log starts showing orders executed at price 0 — silently,
    and in the log used to compute P&L. This fails first instead.
    """
    import pathlib
    import re

    sql = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
    block = re.search(r"CREATE TABLE IF NOT EXISTS trades \((.*?)\n\);", sql, re.S)
    assert block, "no trades table in schema.sql"

    price_line = [ln for ln in block.group(1).splitlines() if ln.strip().startswith("price")]
    assert price_line, "trades has no price column"
    assert "NOT NULL" in price_line[0], (
        "trades.price must stay NOT NULL — lib/tradeStore.server.ts coerces a NULL "
        "to 0, which renders as a trade executed at zero"
    )


async def test_a_persistence_failure_does_not_raise(monkeypatch, caplog):
    """The order is already at the exchange by the time this runs.

    Failing the caller because the database is down would tell the operator their
    order failed when it did not — the worst possible lie on this path.
    """
    monkeypatch.setattr(oe, "get_db_pool", lambda: None)

    with caplog.at_level("ERROR"):
        await oe._persist_operator_trade(
            exchange_order_id="oid-4", symbol="BTC/USDT", side="buy",
            qty=0.1, price=70_000.0, testnet=False,
        )

    assert "NOT persisted" in caplog.text
    assert "exists at the exchange" in caplog.text


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def test_an_unsupported_exchange_is_rejected_before_any_client_is_built():
    from fastapi import HTTPException

    creds = oe.Credentials(apiKey="k", apiSecret="s", testnet=True)
    with pytest.raises(HTTPException) as excinfo:
        oe._build_client("kraken", creds)
    assert excinfo.value.status_code == 400


def test_the_client_is_spot_and_carries_only_this_requests_credentials():
    """NOT the shared `services/exchange_client` singleton.

    That singleton holds the BACKEND's keys and is the agent's path to the venue.
    Reusing it here would let an operator request execute with the agent's
    credentials, or leave the operator's credentials on a long-lived object every
    agent shares.

    `defaultType: spot` is set explicitly because the default differs between
    venues and between ccxt versions — a silent switch to futures would place a
    leveraged order where a spot one was intended.
    """
    creds = oe.Credentials(apiKey="operator-key", apiSecret="operator-secret", testnet=True)
    client = oe._build_client("binance", creds)
    try:
        assert client.apiKey == "operator-key"
        assert client.options.get("defaultType") == "spot"

        from backend.services.exchange_client import get_exchange_client

        assert client is not get_exchange_client(), "must not reuse the agent's client"
    finally:
        # Built but never connected; closing releases the session ccxt creates.
        import asyncio

        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(client.close())
