"""The Binance futures testnet mirror: why it bypasses ccxt, and what guards that.

WHY THIS MODULE EXISTS AT ALL, since it breaks the "no agent talks to an
exchange directly" rule that `services/venue` otherwise enforces absolutely.
Both ccxt routes to Binance futures testnet were measured and both are unusable:

  1. `set_sandbox_mode(True)` raises NotSupported on ccxt 4.5.75.
  2. Overriding `urls['api']` by hand LOOKS like it works and silently reaches
     MAINNET. With every `fapi*` entry repointed, `load_markets()` succeeded and
     `fetch_balance()` returned `-2008 Invalid Api-Key ID` — because it had
     dialled `https://api.binance.com/sapi/v1/capital/config/getall`, a mainnet
     SPOT host no `fapi*` override touches.

So the exception buys a module with ONE hardcoded host and nothing to override.
The first test is the one that matters: it asserts that property against the
source, because the whole justification collapses if a second host appears.

VERIFIED AGAINST THE REAL SANDBOX before any of this was wired in — a full
round trip (open 79.5 XRP @ 1.5099, position present at the venue, reduce-only
close @ 1.509, account flat, balance 5,000.00 -> 4,999.83 on real fees). These
tests are the offline guards; `tests/conftest.py` blocks the network, so the
live proof is a script, exactly as it is for `bybit_testnet_roundtrip.py`.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from backend.services import binance_testnet as bt


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bt.reset()
    monkeypatch.setenv(bt.KEY_VAR, "k" * 40)
    monkeypatch.setenv(bt.SECRET_VAR, "s" * 40)
    yield
    bt.reset()


# ---------------------------------------------------------------------------
# THE safety property
# ---------------------------------------------------------------------------

def test_the_module_contains_exactly_one_host_and_it_is_the_sandbox():
    """THE JUSTIFICATION FOR BYPASSING ccxt IS THAT THERE IS NOTHING TO MISROUTE.

    ccxt's binanceusdm carries 22 url entries across five hosts, and repointing
    the ones you think of leaves signed requests going to the live exchange.
    This module's answer is one constant with no sibling — so this test fails
    the moment that stops being true, which is the moment the exception stops
    being justified.
    """
    # SCANNED WITH COMMENTS AND STRINGS STRIPPED, and the first run of this
    # test is why. It failed on `api.binance.com` — which appears in this
    # module's DOCSTRING, where the mainnet host ccxt wrongly dialled is
    # recorded as the finding. A mention is not a call, and this project has
    # made that exact mistake before (the `analyze_mistake` docstring match,
    # the `EXCHANGE_ID` scan in `test_paper_testnet`). The explanation has to
    # keep naming the host it warns about.
    code = _code_only(inspect.getsource(bt))
    hosts = set(re.findall(r"https://([a-zA-Z0-9.\-]+)", code))
    assert hosts == set(), f"no host literal belongs in code: {hosts}"

    assert bt.BASE_URL == "https://testnet.binancefuture.com"

    # And it is not assembled from configuration, which is the other way a
    # sandbox url becomes a mainnet one.
    assert "EXCHANGE_ID" not in code
    assert "getenv(\"BASE" not in code


def _code_only(src: str) -> str:
    """`src` with every comment and string literal removed.

    `tokenize` rather than a regex, because the thing being guarded against is
    a host appearing in CODE and the docstrings deliberately quote several.
    """
    import io as _io
    import tokenize

    out = []
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_the_base_url_is_assigned_exactly_once():
    """One constant, no sibling, no reassignment — the property the exception
    to the ccxt chokepoint rests on."""
    code_lines = [
        ln for ln in inspect.getsource(bt).splitlines()
        if ln.strip().startswith("BASE_URL")
    ]
    assert len(code_lines) == 1, code_lines
    assert code_lines[0].strip() == 'BASE_URL = "https://testnet.binancefuture.com"'


def test_the_testnet_key_variables_are_separate_from_mainnet():
    """The same asymmetry `_credentials(testnet=)` already enforces for Bybit:
    verifying on a sandbox must never require pasting a testnet key over a live
    one, because putting the live one back is where a real key ends up in play
    by accident."""
    assert bt.KEY_VAR == "BINANCE_TESTNET_API_KEY"
    assert bt.SECRET_VAR == "BINANCE_TESTNET_SECRET"
    src = inspect.getsource(bt)
    assert "BINANCE_API_KEY" not in src
    assert "BINANCE_SECRET" not in src


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("XRP/USDT", "XRPUSDT"),
    ("XRP/USDT:USDT", "XRPUSDT"),      # the ccxt perpetual spelling
    ("xrp/usdt", "XRPUSDT"),
    ("DOGE-USDT", "DOGEUSDT"),
])
def test_both_symbol_spellings_normalise(given, expected):
    """Both reach here: the system uses the display form, ccxt the perpetual
    one, and `paper_testnet` is called from both sides of that boundary.
    Normalising in ONE place is `Venue.resolve_symbol`'s reasoning — thirty
    call sites remembering a suffix is thirty chances to address the wrong
    instrument."""
    assert bt.to_binance_symbol(given) == expected


# ---------------------------------------------------------------------------
# Quantisation — truncate, never round up
# ---------------------------------------------------------------------------

RULES = {"step": 0.1, "minQty": 0.1, "quantityPrecision": 1}


def test_a_size_is_truncated_to_the_step_never_rounded_up():
    """ROUNDING UP WOULD STAKE MORE THAN ANY GATE APPROVED, and
    `tests/test_venue_live_path.py` already learned this the hard way: a
    fixture using `f"{amount:.1f}"` ROUNDED 0.05 to 0.1, cleared the minimum,
    and made a refusal test pass for the wrong reason. A fixture kinder than
    the venue proves nothing; a client kinder than the venue places an order
    larger than was approved."""
    assert bt._quantise(79.69, RULES) == "79.6"
    assert bt._quantise(79.61, RULES) == "79.6"
    assert bt._quantise(0.19, RULES) == "0.1"


def test_a_size_below_the_minimum_is_REFUSED_not_bumped_up():
    assert bt._quantise(0.0001, RULES) is None
    assert bt._quantise(0.09, RULES) is None


def test_the_refusal_names_the_minimum_and_the_step(monkeypatch):
    """A refusal that only says no is what made the operator's sizing feel
    arbitrary once already."""
    async def _rules(client, symbol):
        return RULES

    monkeypatch.setattr(bt, "_instrument_rules", _rules)
    out = asyncio.run(bt.market_order(symbol="XRP/USDT", side="buy", qty=0.0001))
    assert out.ok is False
    assert "minimum" in out.error and "0.1" in out.error
    assert "rounded up" in out.error


def test_a_trigger_price_is_snapped_to_the_venue_tick():
    """FOUND BY PROBING BTCUSDT, AND IT WAS INVISIBLE.

    BTC's tick is 0.10, so an unrounded 82554.57 is not a valid price. The
    conditional order carrying it was refused for an UNRELATED reason (-4120,
    the venue not accepting the type at all), so the filter violation never
    surfaced — a bug that only appears on a venue that accepts the order type,
    which is precisely the one place it would cost a stop.

    ROUNDING IS CORRECT HERE AND TRUNCATION IS NOT, which is the opposite of
    the quantity rule one function above. A trigger moves by at most half a
    tick; refusing a stop over a rounding question would leave the position
    unprotected, and that is the worse failure. A quantity truncates DOWN
    because staking more than was approved is the hazard there.
    """
    btc = {"tick": 0.10, "pricePrecision": 2}
    assert bt._quantise_price(82554.57, btc) == "82554.60"
    assert bt._quantise_price(82554.54, btc) == "82554.50"

    xrp = {"tick": 0.0001, "pricePrecision": 4}
    assert bt._quantise_price(1.46271, xrp) == "1.4627"

    # No filter known: pass it through rather than invent a tick.
    assert float(bt._quantise_price(1.23456, {})) == pytest.approx(1.23456)


def test_the_conditional_order_sends_a_quantised_trigger():
    src = inspect.getsource(bt._conditional)
    assert '"stopPrice": _quantise_price(trigger, rules)' in src


# ---------------------------------------------------------------------------
# The clock, which looks exactly like a bad key
# ---------------------------------------------------------------------------

def test_the_signed_timestamp_is_corrected_by_the_server_offset():
    """THE FIRST PROBE FROM THIS MACHINE FAILED -1021 WITH VALID KEYS, because
    the clock was 33.2 seconds behind. Every signed request would have been
    refused for a reason whose text points at the credentials."""
    src = inspect.getsource(bt._signed)
    assert "await _server_offset(client)" in src
    assert "_1021" in src.replace("-1021", "_1021"), "a -1021 must force a re-measure"


def test_a_stale_clock_is_retried_once_rather_than_surfaced():
    src = inspect.getsource(bt._signed)
    at = src.index('"code":-1021')
    assert "force=True" in src[at:at + 400]


# ---------------------------------------------------------------------------
# The facade the monitor talks to
# ---------------------------------------------------------------------------

def test_the_facade_answers_every_call_the_monitor_makes():
    """`_venue_for(pos)` returns either a real `Venue` or this, and the caller
    must not be able to tell. These five are what `position_monitor` reaches
    for; `paper_testnet` additionally uses the three below them."""
    v = bt.BinanceTestnetVenue()
    for name in ("id", "has_credentials", "place_stop_loss", "place_take_profit",
                 "cancel_order"):
        assert hasattr(v, name), f"the monitor calls venue.{name}"
    for name in ("free_usdt", "ensure_leverage", "market_order"):
        assert hasattr(v, name), f"paper_testnet calls venue.{name}"
    assert v.testnet is True
    assert v.id == "binance_testnet"


def test_the_facade_signatures_match_the_real_venue():
    """A facade whose arguments differ is a crash on the one path that matters.
    Compared by NAME against `services/venue.Venue`, not by eye."""
    from backend.services.venue import Venue

    for name in ("place_stop_loss", "place_take_profit", "cancel_order"):
        real = inspect.signature(getattr(Venue, name))
        fake = inspect.signature(getattr(bt.BinanceTestnetVenue, name))
        real_args = [p for p in real.parameters if p != "self"]
        fake_args = list(fake.parameters)
        assert real_args == fake_args, f"{name}: {real_args} vs {fake_args}"


def test_the_facade_declares_that_it_cannot_rest_orders():
    """MEASURED, NOT ASSUMED. This testnet refuses every conditional type on
    `/fapi/v1/order` with -4120 "use the Algo Order API endpoints instead",
    while `exchangeInfo` ADVERTISES all five. Six parameter shapes produced a
    byte-identical response, and a LIMIT control on the same endpoint returned
    `-2022 ReduceOnly rejected` — the right answer for a flat account — proving
    non-conditional orders arrive fine.

    The flag exists so the monitor reports a limitation it cannot act on ONCE,
    instead of CRITICAL on every fill. An alert that fires every time is one an
    operator learns to ignore."""
    assert bt.BinanceTestnetVenue.supports_resting_orders is False


def test_a_real_venue_still_gets_the_CRITICAL():
    """The half that matters. `getattr(venue, "supports_resting_orders", True)`
    must leave the real-money path untouched."""
    from backend.services.venue import Venue

    assert getattr(Venue, "supports_resting_orders", True) is True

    src = inspect.getsource(
        __import__("backend.agents.position_monitor", fromlist=["x"])
        .PositionMonitorAgent._place_resting_stop
    )
    assert 'getattr(venue, "supports_resting_orders", True)' in src
    assert "logger.critical" in src, "a real venue's refusal must still be CRITICAL"


# ---------------------------------------------------------------------------
# paper_testnet dispatches
# ---------------------------------------------------------------------------

def test_the_mirror_routes_to_binance_by_default(monkeypatch):
    import backend.services.paper_testnet as pt

    monkeypatch.delenv(pt.VENUE_CHOICE_VAR, raising=False)
    assert pt.venue_choice() == "binance"


def test_an_unrecognised_venue_falls_back_rather_than_disabling(monkeypatch, caplog):
    """A typo must not quietly turn the mirror off while the panel still reports
    it on — the `simulation_mode` failure. Both branches are sandboxes, so
    falling back cannot reach mainnet."""
    import backend.services.paper_testnet as pt

    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "bianance")
    assert pt.venue_choice() == pt.DEFAULT_VENUE
    assert any("not one of" in r.message for r in caplog.records)


def test_bybit_is_still_selectable(monkeypatch):
    import backend.services.paper_testnet as pt

    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "bybit")
    assert pt.venue_choice() == "bybit"


def test_get_venue_returns_the_binance_facade_when_chosen(monkeypatch):
    import backend.services.paper_testnet as pt

    pt.reset()
    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "binance")
    v = pt.get_venue()
    assert v.id == "binance_testnet"
    assert v.testnet is True
    pt.reset()


def test_credentials_are_checked_against_the_CHOSEN_venue(monkeypatch):
    """Checking Bybit's variables while routing to Binance would report the
    mirror unusable with perfectly good Binance keys, or usable with none."""
    import backend.services.paper_testnet as pt

    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "binance")
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_SECRET", raising=False)
    assert pt.credentials_present() is True, "Binance keys are set by the fixture"

    monkeypatch.delenv(bt.KEY_VAR)
    assert pt.credentials_present() is False


def test_status_names_the_chosen_venue_and_its_variables(monkeypatch):
    import backend.services.paper_testnet as pt

    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "binance")
    st = pt.status()
    assert st["venue"] == "binance"
    assert st["keyVariable"] == "BINANCE_TESTNET_API_KEY"
    assert "bybit" in st["supportedVenues"] and "binance" in st["supportedVenues"]


# ---------------------------------------------------------------------------
# It must never raise into a trading path
# ---------------------------------------------------------------------------

def test_an_unreachable_venue_returns_a_result_rather_than_raising(monkeypatch):
    """Property 3. A paper account that stops working because a sandbox is down
    is the worse failure."""
    async def _boom(client, method, path, params=None):
        raise RuntimeError("network gone")

    async def _rules(client, symbol):
        return RULES

    monkeypatch.setattr(bt, "_instrument_rules", _rules)
    monkeypatch.setattr(bt, "_signed", _boom)

    with pytest.raises(RuntimeError):
        # The raw helper may raise...
        asyncio.run(bt._signed(None, "GET", "/x"))

    # ...but `paper_testnet.place`, the only caller on a trading path, swallows
    # it and falls back to the simulated fill.
    import backend.services.paper_testnet as pt

    monkeypatch.setenv(pt.VENUE_CHOICE_VAR, "binance")
    monkeypatch.setenv(pt.ENV_VAR, "true")
    pt.reset()
    assert asyncio.run(pt.place(symbol="XRP/USDT", side="buy", qty=10.0)) is None
    pt.reset()


def test_free_usdt_returns_None_not_zero_when_unreadable(monkeypatch):
    """"no money" and "could not ask" are different facts — the same
    distinction `real_account_balance` makes."""
    async def _fail(client, method, path, params=None):
        return False, "503"

    monkeypatch.setattr(bt, "_signed", _fail)
    assert asyncio.run(bt.free_usdt()) is None
