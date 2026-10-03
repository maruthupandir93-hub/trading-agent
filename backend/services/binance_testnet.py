"""Binance USD-M demo futures adapter. Virtual funds, live exchange responses.

Only the documented demo host is reachable; mainnet credentials are never read.
Market orders use /fapi/v1/order; protective orders use /fapi/v1/algoOrder.
See docs/BINANCE_FUTURES_DEMO.md for setup, limitations and verification.
"""

from __future__ import annotations

import json
import math
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
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
BASE_URL = "https://demo-fapi.binance.com"

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
    """Sign only demo requests. Retry a clock rejection once, never an unknown POST."""
    key, secret = _creds()
    if not key or not secret:
        return False, f"no credentials ({KEY_VAR} / {SECRET_VAR} are empty)"
    for attempt in range(2):
        offset = await _server_offset(client)
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + offset
        p["recvWindow"] = RECV_WINDOW_MS
        query = urllib.parse.urlencode(p)
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            r = await client.request(method, f"{BASE_URL}{path}?{query}&signature={signature}",
                                     headers={"X-MBX-APIKEY": key})
        except Exception as exc:
            # Exception strings may contain the signed URL. Never log/return it.
            return False, f"transport error ({type(exc).__name__}); execution status unknown"
        try:
            body = r.json()
        except Exception:
            return False, f"invalid JSON response (HTTP {r.status_code}); execution status unknown"
        if not isinstance(body, (dict, list)):
            return False, "unexpected response shape; execution status unknown"
        code = body.get("code") if isinstance(body, dict) else None
        if str(code) == "-1021" and attempt == 0:
            await _server_offset(client, force=True)
            continue
        if r.status_code >= 300 or (code is not None and str(code).startswith("-")):
            return False, json.dumps(body, ensure_ascii=True)[:500]
        return True, body
    return False, "clock synchronization failed"


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
    if not isinstance(body, list) or not all(isinstance(row, dict) for row in body):
        return None
    for row in body:
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
    if not isinstance(body, list) or not all(isinstance(row, dict) for row in body):
        return None
    for row in body:
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
    if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
        return {}
    try:
        rules: Dict[str, Dict[str, Any]] = {}
        for m in info.get("symbols") or []:
            if not isinstance(m, dict):
                return {}
            entry: Dict[str, Any] = {}
            for f in m.get("filters") or []:
                if not isinstance(f, dict):
                    return {}
                if f.get("filterType") == "LOT_SIZE":
                    entry["step"] = float(f.get("stepSize") or 0)
                    entry["minQty"] = float(f.get("minQty") or 0)
                elif f.get("filterType") == "MIN_NOTIONAL":
                    entry["minNotional"] = float(f.get("notional") or 0)
                elif f.get("filterType") == "PRICE_FILTER":
                    # THE TICK, WHICH A TRIGGER PRICE MUST SIT ON.
                    # Found by probing BTCUSDT: tick is 0.10 and an unrounded
                    # 82554.57 is not a valid price. The conditional order was
                    # refused for an unrelated reason (-4120) so the violation was
                    # INVISIBLE — a bug that only surfaces on a venue that accepts
                    # the order type, which is the one place it would cost a stop.
                    entry["tick"] = float(f.get("tickSize") or 0)
            entry["quantityPrecision"] = m.get("quantityPrecision")
            entry["pricePrecision"] = m.get("pricePrecision")
            rules[m.get("symbol")] = entry
    except (ValueError, TypeError, ArithmeticError):
        logger.warning("Malformed demo instrument filters; orders disabled until readable")
        return {}
    _rules_cache, _rules_at = rules, time.time()
    return rules.get(to_binance_symbol(symbol), {})


def _quantise(qty: float, rules: Dict[str, Any]) -> Optional[str]:
    """Use decimal lot arithmetic; binary floats can turn valid 0.3 into 0.2."""
    try:
        q = Decimal(str(qty))
        step = Decimal(str(rules.get("step") or 0))
        minimum = Decimal(str(rules.get("minQty") or 0))
        if not q.is_finite() or q <= 0:
            return None
        if step > 0:
            q = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        precision = rules.get("quantityPrecision")
        if isinstance(precision, int):
            q = q.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
        if q <= 0 or q < minimum:
            return None
        return format(q, "f")
    except (ValueError, ArithmeticError, TypeError):
        return None


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

def _quantise_price(price: float, rules: Dict[str, Any]) -> str:
    p = Decimal(str(price))
    tick = Decimal(str(rules.get("tick") or 0))
    if not p.is_finite() or p <= 0:
        raise ValueError("trigger price must be positive and finite")
    if tick > 0:
        p = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    precision = rules.get("pricePrecision")
    return f"{p:.{precision}f}" if isinstance(precision, int) else format(p, "f")


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
    if side.lower() not in ("buy", "sell"):
        return TestnetOrder(ok=False, error="side must be buy or sell")
    bsym = to_binance_symbol(symbol)
    async with httpx.AsyncClient(timeout=30.0) as client:
        rules = await _instrument_rules(client, symbol)
        if not rules:
            return TestnetOrder(ok=False, error="instrument filters unavailable; order not sent")
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
            "newOrderRespType": "RESULT",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        client_order_id = client_order_id or f"demo_{uuid.uuid4().hex[:28]}"
        if client_order_id:
            # Binance allows [A-Za-z0-9_-] up to 36.
            params["newClientOrderId"] = client_order_id[:36]

        ok, body = await _signed(client, "POST", "/fapi/v1/order", params)
        if not ok:
            # A timeout/5xx may have executed. Query the SAME client id, never resend.
            if "unknown" in str(body).lower() or "transport" in str(body).lower():
                ok, recovered = await _signed(client, "GET", "/fapi/v1/order",
                    {"symbol": bsym, "origClientOrderId": params["newClientOrderId"]})
                if ok:
                    body = recovered
            if not ok:
                return TestnetOrder(ok=False, error=str(body))
        if not isinstance(body, dict):
            return TestnetOrder(ok=False, error="unexpected order response")

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
            if ok2 and isinstance(back, dict):
                body = back
                filled = _f(back.get("executedQty"))
                avg = _f(back.get("avgPrice"))

    return TestnetOrder(
        ok=True, order_id=order_id or None, average_price=avg, filled_qty=filled, raw=body or {},
    )


def _f(v: Any) -> Optional[float]:
    try:
        out = float(v)
        if not math.isfinite(out):
            return None
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
    if not isinstance(body, dict):
        return {"ok": False, "reason": "unexpected account response"}
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


# Conditional orders have their own Algo Order API, including cancellation.


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
    if side.lower() not in ("buy", "sell"):
        return TestnetOrder(ok=False, error="side must be buy or sell")
    if not _f(trigger) or float(trigger) <= 0:
        return TestnetOrder(ok=False, error="trigger must be positive and finite")
    bsym = to_binance_symbol(symbol)
    async with httpx.AsyncClient(timeout=30.0) as client:
        rules = await _instrument_rules(client, symbol)
        if not rules:
            return TestnetOrder(ok=False, error="instrument filters unavailable; order not sent")
        quantity = _quantise(qty, rules)
        if quantity is None:
            return TestnetOrder(ok=False, error=f"quantity {qty} below {bsym} minimum")

        params: Dict[str, Any] = {
            "symbol": bsym,
            "side": "BUY" if side.lower() == "buy" else "SELL",
            "type": order_type,
            "quantity": quantity,
            "algoType": "CONDITIONAL",
            "triggerPrice": _quantise_price(trigger, rules),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
        }
        if client_order_id:
            params["clientAlgoId"] = client_order_id[:36]
        ok, body = await _signed(client, "POST", "/fapi/v1/algoOrder", params)

    if not ok:
        return TestnetOrder(ok=False, error=str(body))
    if not isinstance(body, dict) or not body.get("algoId"):
        return TestnetOrder(ok=False, error="missing algo order id")
    return TestnetOrder(ok=True, order_id="algo:" + str(body["algoId"]), raw=body)


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
        is_algo = str(order_id).startswith("algo:")
        path = "/fapi/v1/algoOrder" if is_algo else "/fapi/v1/order"
        params = {"algoId": str(order_id)[5:]} if is_algo else {
            "symbol": to_binance_symbol(symbol), "orderId": str(order_id)}
        ok, body = await _signed(client, "DELETE", path, params)
    if ok:
        return True
    try:
        already_gone = json.loads(str(body)).get("code") == -2011
    except (ValueError, AttributeError):
        already_gone = False
    if already_gone:
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
    # Algo orders provide venue-side protection, including when the app stops.
    supports_resting_orders = True

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


_demo_prices: Dict[str, tuple[float, float]] = {}


def demo_data_active() -> bool:
    """Use demo market data only for an explicitly connected paper mirror."""
    from backend.core.config import settings
    from backend.services import paper_testnet
    return (not settings.LIVE_TRADING and paper_testnet.venue_choice() == "binance"
            and paper_testnet.active())


def cached_price(symbol: str) -> float:
    price, observed = _demo_prices.get(to_binance_symbol(symbol), (0.0, 0.0))
    return price if time.monotonic() - observed <= 15 else 0.0


async def fetch_ticker(symbol: str) -> Dict[str, Any]:
    """Actual demo futures quote; failures propagate to the feed's retry loop."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        row = await _get_json(client, "/fapi/v1/ticker/24hr", {"symbol": to_binance_symbol(symbol)})
    price = _f(row.get("lastPrice")) if isinstance(row, dict) else None
    if not price or price <= 0:
        raise ValueError("demo futures ticker has no usable price")
    _demo_prices[to_binance_symbol(symbol)] = (price, time.monotonic())
    return {"last": price, "baseVolume": _f(row.get("volume")) or 0.0}


async def fetch_klines(symbol: str, interval: str, limit: int = 100) -> list:
    """Live demo futures candles, with exchange close timestamps."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            rows = await _get_json(client, "/fapi/v1/klines", {
                "symbol": to_binance_symbol(symbol), "interval": interval,
                "limit": min(max(int(limit), 1), 1500)})
        return [{"openTime": r[0], "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                 "closeTime": r[6]} for r in rows]
    except Exception as exc:
        logger.warning("Demo candles unavailable for %s (%s)", symbol, type(exc).__name__)
        return []
