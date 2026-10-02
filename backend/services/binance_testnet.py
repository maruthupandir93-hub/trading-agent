"""Binance USDⓈ-M FUTURES TESTNET, reached directly — because ccxt cannot.

WHY THIS IS NOT A ccxt VENUE, WHICH IS THE WHOLE REASON THE FILE EXISTS
======================================================================
Everything else that places an order in this project goes through
`services/venue.Venue`, and `ExchangeClient`'s rule is absolute: *"the
Execution API is a hard chokepoint — no agent talks to an exchange directly,
ever."* This module is a deliberate, narrow exception, and it is one because
BOTH ccxt routes to Binance futures testnet are unusable:

1. `set_sandbox_mode(True)` is HARD-REFUSED. Measured on ccxt 4.5.75:

       NotSupported: binanceusdm testnet/sandbox mode is not supported for
       futures anymore, please check the deprecation announcement ...

2. **Overriding `urls['api']` by hand LOOKS like it works and silently reaches
   MAINNET.** This is the dangerous one and it is why the exception is worth
   taking. With every `fapi*` entry repointed at the testnet, `load_markets()`
   succeeded and `fetch_balance()` then failed with

       AuthenticationError: binanceusdm {"code":-2008,"msg":"Invalid Api-Key ID."}

   The key is valid on the testnet, so that error meant the request had gone
   somewhere else. Intercepting the dial showed where:

       https://api.binance.com/sapi/v1/capital/config/getall

   `binanceusdm.fetch_balance` routes through `sapi` — a MAINNET SPOT host that
   no `fapi*` override touches. ccxt's url map has 22 entries across five hosts;
   repointing the ones you happen to think of leaves authenticated requests
   going to the live exchange with whatever keys are configured. A mirror that
   can do that is not a testing aid, which is exactly the reasoning
   `paper_testnet` property 2 already states for Bybit.

So: ONE base url, a module constant, with NO mainnet host anywhere in this file.
There is nothing to override and nothing to get wrong. `tests/` asserts the
absence of any other host as a property of the source.

THE CLOCK SKEW IS NOT A DETAIL — IT LOOKS EXACTLY LIKE A BAD KEY
================================================================
The first signed probe from this machine returned

    {"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}

which reads as a credential problem and is not one: the machine's clock was
**33.2 seconds behind** Binance's server. Every signed request would have failed
with the keys perfectly valid. `_server_offset()` measures the difference once
and re-measures on a cadence, and every signature uses the corrected timestamp.
This is the same class as the `-2008` above — an error whose text sends you to
the wrong file.

WHAT THIS MODULE MAY AND MAY NOT DO
===================================
* It places orders ONLY on the testnet, ONLY for the paper book, and ONLY from
  `paper_testnet.place`, which `ExecutionAgent` consults inside its simulated
  branch. It never becomes a second path for a real trade.
* It NEVER raises into a trading path. Every public function returns a result
  object or None; a testnet outage degrades the mirror to a simulated fill and
  the paper trade still books (`paper_testnet` property 3).
* It never invents a price or a fill quantity (invariant 6). An order the venue
  accepted but did not fill is reported as unfilled, not rounded up to what was
  asked — the mistake this project already made once in `paper_testnet.place`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# THE ONLY HOST IN THIS FILE, AND THAT IS THE SAFETY PROPERTY.
# Not read from the environment, not derived from EXCHANGE_ID, and with no
# mainnet sibling to fall back to. See the module docstring for the measured
# reason ccxt's configurable routing is not safe here.
BASE_URL = "https://testnet.binancefuture.com"

KEY_VAR = "BINANCE_TESTNET_API_KEY"
SECRET_VAR = "BINANCE_TESTNET_SECRET"

# Binance rejects a signed request whose timestamp is outside this window. The
# value is generous on purpose: this is a sandbox and the cost of a rejected
# mirror order is a less faithful paper fill, not a missed trade.
RECV_WINDOW_MS = 10_000

# How often to re-measure the clock difference. A laptop that sleeps can drift
# far in one session, and the failure mode (-1021) does not name the clock.
_OFFSET_TTL_S = 300.0

_offset_ms: int = 0
_offset_at: float = 0.0
_rules_cache: Dict[str, Dict[str, Any]] = {}
_rules_at: float = 0.0
_RULES_TTL_S = 3600.0


@dataclass
class TestnetOrder:
    """What came back, or why nothing did. Mirrors `venue.OrderResult`'s shape
    so `paper_testnet` can treat both mirror venues identically."""

    ok: bool
    order_id: Optional[str] = None
    average_price: Optional[float] = None
    filled_qty: Optional[float] = None
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def credentials_present() -> bool:
    """Whether both variables are set. Read at CALL time, like every toggle."""
    return bool((os.getenv(KEY_VAR) or "").strip() and (os.getenv(SECRET_VAR) or "").strip())


def _creds() -> tuple[str, str]:
    return (os.getenv(KEY_VAR) or "").strip(), (os.getenv(SECRET_VAR) or "").strip()


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------

def to_binance_symbol(symbol: str) -> str:
    """`XRP/USDT` or `XRP/USDT:USDT` -> `XRPUSDT`.

    Both spellings reach here because the rest of the system uses the display
    form while ccxt uses the perpetual form, and `paper_testnet` is called from
    both sides of that boundary. Normalising in ONE place is the same reasoning
    `Venue.resolve_symbol` gives: thirty call sites remembering a suffix is
    thirty chances to address the wrong instrument.
    """
    return symbol.split(":")[0].replace("/", "").replace("-", "").upper()


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

async def _get_json(client: httpx.AsyncClient, path: str, params: Optional[Dict] = None):
    r = await client.get(BASE_URL + path, params=params or {})
    r.raise_for_status()
    return r.json()


async def _server_offset(client: httpx.AsyncClient, *, force: bool = False) -> int:
    """Binance server time minus local time, in ms. Cached.

    Measured rather than assumed: this machine was 33.2s behind, and every
    signed request fails `-1021` without the correction while reporting
    something that reads like an authentication problem.
    """
    global _offset_ms, _offset_at
    if not force and (time.time() - _offset_at) < _OFFSET_TTL_S:
        return _offset_ms
    try:
        data = await _get_json(client, "/fapi/v1/time")
        _offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
        _offset_at = time.time()
        if abs(_offset_ms) > 1000:
            logger.info(
                "Binance testnet clock offset %+.1fs — signed requests are corrected "
                "for it. Without the correction every order fails -1021, which reads "
                "as a bad API key.", _offset_ms / 1000,
            )
    except Exception as exc:  # noqa: BLE001 - an unmeasurable clock is not fatal
        logger.warning("Could not read Binance testnet server time (%s); using 0 offset.", exc)
    return _offset_ms


async def _signed(
    client: httpx.AsyncClient, method: str, path: str, params: Optional[Dict] = None,
) -> tuple[bool, Any]:
    """One signed request. Returns (ok, payload-or-error-text). Never raises."""
    key, secret = _creds()
    if not key or not secret:
        return False, f"no credentials ({KEY_VAR} / {SECRET_VAR} are empty)"

    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000) + await _server_offset(client)
    p["recvWindow"] = RECV_WINDOW_MS
    query = urllib.parse.urlencode(p)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}{path}?{query}&signature={signature}"

    try:
        r = await client.request(method, url, headers={"X-MBX-APIKEY": key})
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    if r.status_code >= 400:
        body = r.text[:300]
        # A stale clock is self-healing and worth retrying ONCE, because the
        # alternative is an order refused for a reason that has nothing to do
        # with the order.
        if '"code":-1021' in body:
            await _server_offset(client, force=True)
            return await _signed(client, method, path, params)
        return False, body
    try:
        return True, r.json()
    except Exception:  # noqa: BLE001
        return False, r.text[:300]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

async def free_usdt() -> Optional[float]:
    """Available USDT, or None when it cannot be read.

    None NEVER 0.0 — "no money" and "could not ask" are different facts, and the
    same distinction `real_account_balance` already makes.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        ok, body = await _signed(client, "GET", "/fapi/v2/balance")
    if not ok:
        logger.warning("Binance testnet balance unreadable: %s", body)
        return None
    for row in body or []:
        if row.get("asset") == "USDT":
            try:
                return float(row.get("availableBalance"))
            except (TypeError, ValueError):
                return None
    return None


async def open_position(symbol: str) -> Optional[Dict[str, Any]]:
    """The live position for one symbol, or None. `{}` means genuinely flat."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        ok, body = await _signed(
            client, "GET", "/fapi/v2/positionRisk", {"symbol": to_binance_symbol(symbol)},
        )
    if not ok:
        logger.warning("Binance testnet position unreadable for %s: %s", symbol, body)
        return None
    for row in body or []:
        try:
            amt = float(row.get("positionAmt") or 0.0)
        except (TypeError, ValueError):
            continue
        if amt != 0.0:
            return {
                "symbol": symbol,
                "qty": abs(amt),
                "side": "buy" if amt > 0 else "sell",
                "entryPrice": float(row.get("entryPrice") or 0.0),
                "unrealizedPnl": float(row.get("unRealizedProfit") or 0.0),
            }
    return {}


async def _instrument_rules(client: httpx.AsyncClient, symbol: str) -> Dict[str, Any]:
    """Lot step, min qty and min notional for one perpetual. Cached for an hour.

    WITHOUT THESE AN ORDER IS REFUSED, not rounded. Binance rejects a quantity
    that is not a multiple of the step size, and this project's rule is that a
    size below the minimum is REFUSED rather than bumped up — rounding up would
    stake more than any gate approved.
    """
    global _rules_cache, _rules_at
    if _rules_cache and (time.time() - _rules_at) < _RULES_TTL_S:
        return _rules_cache.get(to_binance_symbol(symbol), {})
    try:
        info = await _get_json(client, "/fapi/v1/exchangeInfo")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance testnet exchangeInfo unavailable (%s).", exc)
        return {}
    rules: Dict[str, Dict[str, Any]] = {}
    for m in info.get("symbols") or []:
        entry: Dict[str, Any] = {}
        for f in m.get("filters") or []:
            if f.get("filterType") == "LOT_SIZE":
                entry["step"] = float(f.get("stepSize") or 0)
                entry["minQty"] = float(f.get("minQty") or 0)
            elif f.get("filterType") == "MIN_NOTIONAL":
                entry["minNotional"] = float(f.get("notional") or 0)
        entry["quantityPrecision"] = m.get("quantityPrecision")
        rules[m.get("symbol")] = entry
    _rules_cache, _rules_at = rules, time.time()
    return rules.get(to_binance_symbol(symbol), {})


def _quantise(qty: float, rules: Dict[str, Any]) -> Optional[str]:
    """Trim to the venue's step. Returns None when the result is below minimum.

    TRUNCATES, never rounds up. `tests/test_venue_live_path.py` learned this the
    hard way: a fixture that rounded 0.05 to 0.1 cleared the minimum and made a
    refusal test pass for the wrong reason. A fixture kinder than the venue
    proves nothing, and a client kinder than the venue places an order larger
    than was approved.
    """
    step = float(rules.get("step") or 0.0)
    min_qty = float(rules.get("minQty") or 0.0)
    q = float(qty)
    if step > 0:
        q = int(q / step) * step
    precision = rules.get("quantityPrecision")
    text = f"{q:.{int(precision)}f}" if isinstance(precision, int) else repr(q)
    if float(text) <= 0 or (min_qty and float(text) < min_qty):
        return None
    return text


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

async def ensure_leverage(symbol: str, leverage: int) -> bool:
    """Best effort. A refusal does NOT abort a mirrored order.

    DELIBERATELY more permissive than the live path, and the asymmetry is the
    same one `paper_testnet` states: on mainnet, filling at a leverage we know
    is wrong is trading on a false number and the trade aborts; on a testnet the
    position is play money and a less-faithful mirror still beats no mirror.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        ok, body = await _signed(
            client, "POST", "/fapi/v1/leverage",
            {"symbol": to_binance_symbol(symbol), "leverage": int(leverage)},
        )
    if not ok:
        logger.info("Binance testnet would not set %sx on %s (%s). Continuing.",
                    leverage, symbol, body)
    return bool(ok)


async def market_order(
    *,
    symbol: str,
    side: str,
    qty: float,
    reduce_only: bool = False,
    client_order_id: Optional[str] = None,
) -> TestnetOrder:
    """One market order on the testnet. Never raises.

    `reduceOnly` IS WHAT MAKES A CLOSE A CLOSE. Without it the order is merely
    an opposite-side market order, and any surplus over the live size OPENS a
    position the other way — the same reason `venue.market_order` sets it.
    """
    bsym = to_binance_symbol(symbol)
    async with httpx.AsyncClient(timeout=30.0) as client:
        rules = await _instrument_rules(client, symbol)
        quantity = _quantise(qty, rules)
        if quantity is None:
            return TestnetOrder(
                ok=False,
                error=(
                    f"quantity {qty} is below {bsym}'s minimum "
                    f"({rules.get('minQty')}) or rounds to zero at step "
                    f"{rules.get('step')} — refused rather than rounded up"
                ),
            )

        params: Dict[str, Any] = {
            "symbol": bsym,
            "side": "BUY" if side.lower() == "buy" else "SELL",
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_order_id:
            # Binance allows [A-Za-z0-9_-] up to 36.
            params["newClientOrderId"] = client_order_id[:36]

        ok, body = await _signed(client, "POST", "/fapi/v1/order", params)
        if not ok:
            return TestnetOrder(ok=False, error=str(body))

        # A MARKET order's ack may not carry the fill. Ask for the order back,
        # because `avgPrice` is the only honest source of what it cost and
        # booking the requested price against a real order id would attach a
        # made-up fill to a real trade (invariant 6).
        order_id = str(body.get("orderId") or "")
        filled = _f(body.get("executedQty"))
        avg = _f(body.get("avgPrice"))
        if (not avg or avg <= 0) and order_id:
            ok2, back = await _signed(
                client, "GET", "/fapi/v1/order", {"symbol": bsym, "orderId": order_id},
            )
            if ok2:
                body = back
                filled = _f(back.get("executedQty"))
                avg = _f(back.get("avgPrice"))

    return TestnetOrder(
        ok=True, order_id=order_id or None, average_price=avg, filled_qty=filled, raw=body or {},
    )


def _f(v: Any) -> Optional[float]:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out


async def verify() -> Dict[str, Any]:
    """Prove the credentials work with a REAL authenticated call. Never raises.

    A key that merely EXISTS proves nothing — it can be revoked, lack trade
    permission, or be a MAINNET key pasted into the testnet slot, and all three
    look identical until an order is refused.
    """
    if not credentials_present():
        return {
            "ok": False,
            "reason": (
                f"No testnet credentials. Set {KEY_VAR} and {SECRET_VAR} in .env — "
                f"they are deliberately SEPARATE from the mainnet pair so verifying "
                f"never requires pasting a testnet key over a live one."
            ),
        }
    async with httpx.AsyncClient(timeout=20.0) as client:
        offset = await _server_offset(client, force=True)
        ok, body = await _signed(client, "GET", "/fapi/v2/account")
    if not ok:
        return {"ok": False, "reason": str(body), "clockOffsetMs": offset}
    try:
        balance = float(body.get("availableBalance") or body.get("totalWalletBalance") or 0.0)
    except (TypeError, ValueError):
        balance = 0.0
    return {
        "ok": bool(body.get("canTrade")),
        "balanceUsdt": balance,
        "canTrade": bool(body.get("canTrade")),
        "clockOffsetMs": offset,
        "reason": (
            f"Authenticated against Binance futures TESTNET. Available "
            f"{balance:,.2f} USDT, canTrade={body.get('canTrade')}. "
            + (f"This machine's clock is {offset/1000:+.1f}s from the server and every "
               f"signed request is corrected for it." if abs(offset) > 1000 else "")
        ),
    }


def reset() -> None:
    """Drop the cached clock offset and instrument rules. For tests."""
    global _offset_ms, _offset_at, _rules_cache, _rules_at
    _offset_ms, _offset_at, _rules_cache, _rules_at = 0, 0.0, {}, 0.0


# ---------------------------------------------------------------------------
# The resting legs, so a mirrored position is protected AT the venue
# ---------------------------------------------------------------------------
#
# WHY THESE EXIST HERE AT ALL. `PositionMonitorAgent._venue_backed` asks "is
# there a real exchange order behind this position?", and with the mirror on a
# paper position genuinely exists on the testnet. CLAUDE.md states the
# consequence of getting that wrong: leaving the old `tab != "real"` guard
# "would open a real testnet position with NO stop at the venue — the exact gap
# the resting stop exists to close — and would make the test unfaithful in the
# one direction that matters: it would look safer than the real thing."
#
# So this client has to answer the same five calls the monitor makes on a
# venue: `id`, `has_credentials`, `place_stop_loss`, `place_take_profit` and
# `cancel_order`. The signatures match `services/venue.Venue` exactly, because
# `_venue_for(pos)` returns one or the other and the caller must not care.


# MEASURED 2026-10-03: THIS TESTNET REFUSES EVERY CONDITIONAL ORDER TYPE.
#
# `exchangeInfo` ADVERTISES them for XRPUSDT —
#     ['LIMIT','MARKET','STOP','STOP_MARKET','TAKE_PROFIT',
#      'TAKE_PROFIT_MARKET','TRAILING_STOP_MARKET']
# — and `/fapi/v1/order` then refuses all five conditional ones identically:
#     {"code":-4120,"msg":"Order type not supported for this endpoint.
#                          Please use the Algo Order API endpoints instead."}
#
# Six parameter shapes were tried (quantity+reduceOnly, with and without
# workingType, closePosition=true, quantity alone, STOP as a limit with
# price+stopPrice, TRAILING_STOP_MARKET) and the response was byte-identical,
# so it is the ENDPOINT, not the parameters. A LIMIT control on the same
# endpoint returned `-2022 ReduceOnly Order is rejected` — the correct answer
# for a flat account — which proves non-conditional orders arrive fine.
#
# A CAPABILITY LIST THAT SAYS YES WHILE THE ENDPOINT SAYS NO is the same shape
# as this project's other "advertised but dead" findings (gpt-oss-20b still in
# `GET /v1/models`, Binance futures sandbox still in ccxt's url map). The
# reason it is written down here is so the next reader does not spend an hour
# on parameters.
#
# CONSEQUENCE, STATED PLAINLY: a MIRRORED paper position has no protective
# order resting at the venue. Its stop is enforced by `PositionMonitorAgent`
# on every tick, exactly as it is for an unmirrored paper position — so the
# mirror is no WORSE protected than paper already was, and the entry, the
# close, the real fill price, the step truncation and the real fees are all
# still faithful. `supports_resting_orders = False` on the facade lets the
# monitor report this as a known limitation instead of a CRITICAL fault on
# every single fill.


async def _conditional(
    *, symbol: str, side: str, qty: float, trigger: float, order_type: str,
    client_order_id: Optional[str] = None,
) -> TestnetOrder:
    """STOP_MARKET / TAKE_PROFIT_MARKET, reduce-only, triggered on MARK price.

    MARK PRICE, not last. A thin book can move the last price on a single print,
    and this project already set mark-price triggers on Bybit while leaving
    every Binance stop on last — the asymmetry was a real bug, and this client
    is not going to reintroduce it on the venue it was wrong on.

    REDUCE-ONLY for the reason a close is reduce-only: without it a trigger
    order is just an opposite-side market order, and any surplus over the live
    size OPENS a position the other way — on a flat account, that is an order
    to open a reversed position the next time price touches the trigger.
    """
    bsym = to_binance_symbol(symbol)
    async with httpx.AsyncClient(timeout=30.0) as client:
        rules = await _instrument_rules(client, symbol)
        quantity = _quantise(qty, rules)
        if quantity is None:
            return TestnetOrder(ok=False, error=f"quantity {qty} below {bsym} minimum")

        params: Dict[str, Any] = {
            "symbol": bsym,
            "side": "BUY" if side.lower() == "buy" else "SELL",
            "type": order_type,
            "quantity": quantity,
            "stopPrice": trigger,
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]
        ok, body = await _signed(client, "POST", "/fapi/v1/order", params)

    if not ok:
        return TestnetOrder(ok=False, error=str(body))
    return TestnetOrder(ok=True, order_id=str(body.get("orderId") or "") or None, raw=body or {})


async def place_stop_loss(
    *, symbol: str, side: str, qty: float, stop_price: float,
    client_order_id: Optional[str] = None,
) -> TestnetOrder:
    """`side` is the EXIT side — a long's stop SELLS.

    On the entry side it would ADD to the position at the stop rather than close
    it, which is the first of the three properties `tests/test_resting_stop.py`
    pins for the live path.
    """
    return await _conditional(
        symbol=symbol, side=side, qty=qty, trigger=stop_price,
        order_type="STOP_MARKET", client_order_id=client_order_id,
    )


async def place_take_profit(
    *, symbol: str, side: str, qty: float, take_profit_price: float,
    client_order_id: Optional[str] = None,
) -> TestnetOrder:
    """The mirror of the stop. A refused TP is a WARNING, not CRITICAL.

    The asymmetry is deliberate and is the live path's: an unprotected downside
    is a loss that runs, while a missed target is only an upside not captured,
    and the in-process monitor still takes it the moment the process is alive.
    """
    return await _conditional(
        symbol=symbol, side=side, qty=qty, trigger=take_profit_price,
        order_type="TAKE_PROFIT_MARKET", client_order_id=client_order_id,
    )


async def cancel_order(order_id: str, symbol: str) -> bool:
    """Cancel one resting order. Returns whether it is gone.

    A stop left resting on a flat account is an order to OPEN a reversed
    position the next time price touches it, so a failed cancel is worth the
    caller keeping the id and retrying — which is what the monitor does.

    An order that is ALREADY gone counts as cancelled: `-2011 Unknown order`
    means there is nothing left to leak, and reporting failure would make the
    monitor hold an id forever for an order that does not exist.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        ok, body = await _signed(
            client, "DELETE", "/fapi/v1/order",
            {"symbol": to_binance_symbol(symbol), "orderId": str(order_id)},
        )
    if ok:
        return True
    if '"code":-2011' in str(body):
        logger.info("Binance testnet order %s was already gone (%s).", order_id, symbol)
        return True
    logger.warning("Binance testnet could NOT cancel %s on %s: %s", order_id, symbol, body)
    return False


class BinanceTestnetVenue:
    """A `services/venue.Venue`-shaped facade over this module.

    `PositionMonitorAgent._venue_for(pos)` returns either a real `Venue` or
    this, and the caller must not be able to tell — so the five methods it
    reaches for (`id`, `has_credentials`, `place_stop_loss`,
    `place_take_profit`, `cancel_order`) carry the same names and signatures,
    and `paper_testnet` additionally uses `free_usdt`, `ensure_leverage` and
    `market_order`.

    It is a thin facade rather than a subclass on purpose: `Venue.__init__`
    builds a ccxt client, and ccxt is exactly what cannot reach this endpoint.
    """

    id = "binance_testnet"
    testnet = True
    key_variable = KEY_VAR
    # READ BY `position_monitor` so a refusal it cannot do anything about is
    # logged once as a limitation rather than CRITICAL on every fill. A real
    # `Venue` has no such attribute and `getattr(..., True)` is the default, so
    # the live path's CRITICAL is untouched — which is the half that matters.
    supports_resting_orders = False

    @staticmethod
    def has_credentials() -> bool:
        return credentials_present()

    free_usdt = staticmethod(free_usdt)
    ensure_leverage = staticmethod(ensure_leverage)
    market_order = staticmethod(market_order)
    place_stop_loss = staticmethod(place_stop_loss)
    place_take_profit = staticmethod(place_take_profit)
    cancel_order = staticmethod(cancel_order)
    open_positions = staticmethod(open_position)
