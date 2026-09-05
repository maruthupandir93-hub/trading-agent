"""One venue-neutral adapter for Binance and Bybit — and the parameters each needs.

WHY THIS EXISTS
===============
`exchange_client.py` hardcoded `ccxt.binance(...)` and sent market orders with
nothing but a `clientOrderId`. That is enough to open a position and not enough to
trade real money correctly. Four things were missing, and each is a way to lose
money rather than merely a rough edge:

  1. LEVERAGE WAS NEVER SET ON THE VENUE. The Risk Gateway sizes a position for a
     leverage the operator chose, and nothing ever told the exchange about it. The
     account traded at whatever leverage was last set in the venue's UI — possibly
     20x when the agent had sized for 3x. Every margin and liquidation figure the
     system computed was then describing a position that did not exist.

  2. CLOSES DID NOT SEND `reduceOnly`. A close is "send the opposite side for the
     position's size". If the size is even slightly stale — a partial fill, a fee
     deducted in the base asset, a stop that already trimmed it — the surplus does
     not close anything, it OPENS a new position the other way. The operator asked
     to flatten and is now short.

  3. QUANTITIES WERE NOT ROUNDED TO THE VENUE'S FILTERS. Every contract has a
     `stepSize`, a minimum quantity and a minimum notional. An unrounded float is
     rejected outright, and the agent's own log said "order rejected" with no
     indication that the number itself was the problem.

  4. NOTHING COMPARED THE LOCAL BOOK TO THE VENUE'S. The agent's idea of what it
     held came only from its own fills. A manual close in the Binance app, a
     liquidation, or an ADL left the two silently disagreeing, with the stop-loss
     monitor guarding a position that no longer existed.

TWO CLIENTS, AND THAT SPLIT IS THE POINT
========================================
`public` carries NO credentials; `private` carries them.

Market data — tickers, candles, order books, market metadata — goes through
`public`. Binance and Bybit both serve those unauthenticated, and routing them
through a keyed client spends the operator's API-key rate budget on data that
needs no key. That budget is what places orders; exhausting it on a price poll
means the order that matters is the one that gets throttled.

Only `fetch_balance`, `fetch_positions`, `set_leverage` and order placement use
`private`.

WHAT DIFFERS BETWEEN THE TWO VENUES
===================================
ccxt unifies most of it. These are the places it does not, and each one is a
rejected order or a wrong position if it is got wrong:

  POSITION MODE
    Binance one-way : send neither `positionSide` nor anything else; `reduceOnly`
                      is accepted on a close.
    Binance hedge   : `positionSide` = LONG|SHORT is REQUIRED, and `reduceOnly` is
                      REJECTED — Binance returns "Parameter reduceOnly sent when
                      not required". You close by sending the opposite side with
                      the SAME positionSide.
    Bybit one-way   : `positionIdx` = 0, `reduceOnly` accepted.
    Bybit hedge     : `positionIdx` = 1 for the long leg, 2 for the short leg.

    So `reduceOnly` and `positionSide` are not independent flags to set defensively
    — on Binance hedge mode, sending both is an error. `_order_params` encodes the
    matrix rather than leaving each call site to remember it.

  CATEGORY
    Bybit v5 splits its API by `category` (linear / inverse / spot / option).
    ccxt infers it from the market for most calls, and `defaultType: 'swap'` plus
    `defaultSubType: 'linear'` pins it for the ones where it cannot.

  LEVERAGE
    Binance takes one number. Bybit v5 takes buyLeverage and sellLeverage and
    rejects the call outright with `110043 leverage not modified` when the value
    is already set — which is a SUCCESS for our purposes, not a failure, and is
    treated as one below.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
=========================================
It does not decide direction, size or stop distance. Those come from the Risk
Gateway and the Supervisor, and a venue adapter that adjusted them would be making
trading decisions from inside the plumbing. It rounds a size to what the venue can
accept and REPORTS when rounding changed it materially; it never invents one.

VERIFICATION HONESTY
====================
Order placement against Binance mainnet cannot be tested without placing real
orders with real funds, so it is not tested that way here. What IS verified:
the parameter matrix (unit tests, no network), and the live round trip against
Bybit's testnet, which — unlike Binance futures testnet, which ccxt no longer
supports — actually works.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)

SUPPORTED = ("binance", "bybit")


def configured_venue() -> str:
    """Which exchange this process trades on. `EXCHANGE_ID`, defaulting to binance.

    Validated rather than passed through: an unknown id would surface as an
    AttributeError from inside ccxt at the first order, which is the worst moment
    to discover a typo in a config file.
    """
    raw = (os.getenv("EXCHANGE_ID") or "binance").strip().lower()
    if raw not in SUPPORTED:
        logger.error(
            "EXCHANGE_ID=%r is not supported (%s). Falling back to binance.",
            raw, ", ".join(SUPPORTED),
        )
        return "binance"
    return raw


def key_variable(venue: str, *, testnet: bool = False) -> str:
    """The environment variable this venue/network reads its key from.

    Exists so the Settings panel and every error message name the variable the
    operator must actually set, rather than the mainnet one while the process is
    signing testnet requests.
    """
    prefix = "BYBIT" if venue == "bybit" else "BINANCE"
    return f"{prefix}_TESTNET_API_KEY" if testnet else f"{prefix}_API_KEY"


def _credentials(venue: str, *, testnet: bool = False) -> tuple[str, str]:
    """Per-venue, per-NETWORK keys.

    THE VENUE-SPECIFIC NAMES MATTER. Binance and Bybit are different accounts
    holding different money. A single `API_KEY` pair shared between them would
    authenticate against whichever venue happened to be configured, and a key that
    is invalid there fails closed — but a key that is valid on the WRONG venue
    would trade the wrong account. Separate variables make that impossible.

    THE NETWORK SPLIT IS THE SAME ARGUMENT ONE LEVEL DOWN. A venue's testnet keys
    are different credentials against a different account with fake money. Holding
    both means verifying on testnet does not require pasting testnet keys over the
    mainnet ones and putting them back afterwards — and that put-them-back step is
    exactly where a real key ends up in play by accident.

    THE FALLBACK IS DELIBERATELY ONE-DIRECTIONAL. Testnet falls back to the
    mainnet variable (that is the arrangement that existed before this split, and
    a mainnet key sent to a sandbox endpoint is REFUSED — it fails closed). Mainnet
    never reads the testnet variable, so a testnet key can never be reached by a
    client that is about to spend real money.
    """
    prefix = "BYBIT" if venue == "bybit" else "BINANCE"
    if testnet:
        key = os.getenv(f"{prefix}_TESTNET_API_KEY", "").strip()
        secret = os.getenv(f"{prefix}_TESTNET_SECRET", "").strip()
        if key and secret:
            return (key, secret)
    return (os.getenv(f"{prefix}_API_KEY", "").strip(), os.getenv(f"{prefix}_SECRET", "").strip())


@dataclass
class OrderResult:
    """What came back from the venue, or why nothing did.

    `ok=False` NEVER carries a fill. This mirrors `exchange_client.create_market_order`'s
    hard-won rule: that method once fabricated a $60,000 fill on any exception, and
    the caller wrote it into the trade log as real. A result object makes the
    failure impossible to mistake for a fill, because there is no price on it.
    """

    ok: bool
    order_id: Optional[str] = None
    filled_qty: Optional[float] = None
    average_price: Optional[float] = None
    raw: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Set when the venue's filters changed the requested size. The caller must
    # book what was FILLED, not what it asked for.
    requested_qty: Optional[float] = None
    adjusted_qty: Optional[float] = None


@dataclass
class SizingCheck:
    """Whether a size is placeable, and what it becomes after the venue's filters."""

    ok: bool
    qty: float
    reason: Optional[str] = None
    min_qty: Optional[float] = None
    min_notional: Optional[float] = None


class Venue:
    """A single exchange, with a keyless public client and a keyed private one."""

    def __init__(self, venue_id: Optional[str] = None, *, testnet: Optional[bool] = None):
        self.id = venue_id or configured_venue()

        # Defaults to testnet: anything that can move real money defaults to the
        # safe value and the operator opts IN to mainnet, never falls into it.
        #
        # RESOLVED BEFORE THE CREDENTIALS, because which network this client talks
        # to decides which key pair it is allowed to read. Reading the keys first
        # would hand a mainnet key to a sandbox client and vice versa.
        if testnet is None:
            testnet = (os.getenv("USE_TESTNET", "true").lower() == "true")
        self.testnet = testnet

        api_key, secret = _credentials(self.id, testnet=testnet)
        self._api_key = api_key
        self._secret = secret
        self.key_variable = key_variable(self.id, testnet=testnet)

        options = self._venue_options()
        klass = getattr(ccxt, self.id)

        # PUBLIC: no credentials at all, so a price poll cannot spend the key's
        # rate budget or, if a call is ever mis-signed, leak a key into a log.
        self.public = klass({"enableRateLimit": True, "options": dict(options)})
        self.private = klass({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": dict(options),
        })

        if testnet:
            # Bybit's testnet works through ccxt's sandbox mode and is a genuine
            # venue to verify against. Binance's futures testnet does NOT — ccxt
            # reports it unsupported and every private call fails. That is
            # recorded in `exchange_client` and is why LIVE_TRADING, not
            # USE_TESTNET, is the gate that actually prevents real orders.
            for client in (self.public, self.private):
                try:
                    client.set_sandbox_mode(True)
                except Exception as exc:  # pragma: no cover - venue dependent
                    logger.warning("%s: sandbox mode unavailable (%s)", self.id, exc)
            if self.id == "binance":
                logger.warning(
                    "Binance futures testnet is NOT supported through ccxt: private calls "
                    "will fail. USE_TESTNET is not a safety gate — LIVE_TRADING=false is."
                )
        else:
            logger.warning(
                "%s initialised against MAINNET. Orders placed through this client use REAL FUNDS.",
                self.id,
            )

        self._markets: Optional[Dict[str, Any]] = None
        self._markets_lock = asyncio.Lock()
        # Cached because it costs a private call and changes only when the
        # operator changes it in the venue's UI, which is rare and deliberate.
        self._hedge_mode: Optional[bool] = None
        # Symbols whose leverage this process has already set. Re-sending it on
        # every order is a wasted private call, and on Bybit an outright error.
        self._leverage_set: Dict[str, int] = {}
        # caller's symbol -> this venue's perpetual market key. "" is a cached
        # NEGATIVE result: the market list does not change while we run, so a
        # symbol that has no perpetual today will not grow one.
        self._resolved: Dict[str, str] = {}

    # -- config ---------------------------------------------------------

    def _venue_options(self) -> Dict[str, Any]:
        if self.id == "bybit":
            # Bybit v5 is split by `category`. `swap` + `linear` pins USDT
            # perpetuals for the calls where ccxt cannot infer it from a symbol.
            return {"defaultType": "swap", "defaultSubType": "linear"}
        # Without defaultType, ccxt's binance defaults to SPOT — orders would go
        # to a different market than every log line in this system claims.
        return {"defaultType": "future"}

    def has_credentials(self) -> bool:
        return bool(self._api_key and self._secret)

    # -- market metadata (PUBLIC) ---------------------------------------

    async def markets(self) -> Dict[str, Any]:
        """Load and cache market metadata. PUBLIC — no key is needed to read it.

        Locked because several coroutines reach this at once on the first order of
        a run, and `load_markets` is an expensive call that would otherwise be made
        three times concurrently.
        """
        if self._markets is not None:
            return self._markets
        async with self._markets_lock:
            if self._markets is None:
                self._markets = await self.public.load_markets()
                # The private client needs the same metadata to build order
                # params, and copying it avoids a second network fetch.
                self.private.markets = self.public.markets
                self.private.markets_by_id = self.public.markets_by_id
                self.private.symbols = self.public.symbols
                self.private.currencies = self.public.currencies
        return self._markets

    async def market(self, symbol: str) -> Optional[Dict[str, Any]]:
        resolved = await self.resolve_symbol(symbol)
        if resolved is None:
            return None
        return (await self.markets()).get(resolved)

    # -- symbol resolution ------------------------------------------------
    #
    # THE AGENT SAYS "SOL/USDT" AND THAT KEY IS A SPOT MARKET.
    #
    # ccxt's market dict holds BOTH `SOL/USDT` (spot) and `SOL/USDT:USDT` (the
    # linear perpetual). Every caller in this system passes the first form, and
    # `check_size` looked it up with a plain `dict.get` — so the filters applied
    # were the SPOT contract's. Measured against real metadata:
    #
    #   Bybit  SOL/USDT   spot minAmount 0.001   vs  swap minAmount 0.1
    #   Binance BTC/USDT  spot minCost    5.0    vs  swap minCost   50.0
    #
    # A size that cleared our check was therefore rejected by the venue, and the
    # agent's log said only "order rejected".
    #
    # WORSE, THE TWO VENUES DISAGREE ABOUT WHERE THE ORDER GOES. ccxt's binance
    # honours `defaultType: 'future'` inside `market()`, so `SOL/USDT` resolves to
    # the swap there. Bybit's does NOT — `market('SOL/USDT')` returns the spot
    # market despite `defaultType: 'swap'`. So the same call placed a perpetual
    # order on one venue and a SPOT order on the other, while `positionIdx`,
    # `reduceOnly`, the leverage call, the stop and reconciliation all described a
    # perpetual.
    #
    # Resolution happens HERE rather than at the call sites: a symbol form is a
    # venue detail, and thirty callers remembering to append `:USDT` is thirty
    # chances to place a spot order with real money.
    #
    # IT NEVER FALLS BACK TO SPOT. A spot fill is a different instrument — no
    # leverage, no reduce-only close, no position to place a stop against. If the
    # perpetual cannot be found the honest answer is a refusal.

    async def resolve_symbol(self, symbol: str) -> Optional[str]:
        """The caller's symbol -> this venue's linear-perpetual market key."""
        cached = self._resolved.get(symbol)
        if cached is not None:
            return cached or None

        markets = await self.markets()

        if ":" in symbol:
            candidates = [symbol]
        else:
            quote = symbol.split("/")[-1] if "/" in symbol else "USDT"
            # The contract form FIRST. When both exist, the perpetual is the one
            # this system means.
            candidates = [f"{symbol}:{quote}", symbol]

        chosen: Optional[str] = None
        for candidate in candidates:
            mkt = markets.get(candidate)
            if mkt is None:
                continue
            if mkt.get("swap") or mkt.get("contract"):
                chosen = candidate
                break
            # Metadata that names no type at all is accepted rather than assumed
            # spot; only an EXPLICIT spot market is refused below.
            if mkt.get("type") != "spot" and chosen is None:
                chosen = candidate

        if chosen is None:
            spot_only = (markets.get(symbol) or {}).get("type") == "spot"
            logger.error(
                "%s: %s has no linear perpetual market%s. Refusing rather than falling "
                "back to spot — a spot fill carries no leverage, cannot be closed "
                "reduce-only and has no position to rest a stop against.",
                self.id, symbol, " (only a spot market exists)" if spot_only else "",
            )
            self._resolved[symbol] = ""  # negative-cached; the market list is static
            return None

        self._resolved[symbol] = chosen
        if chosen != symbol:
            logger.debug("%s: %s resolves to the perpetual %s", self.id, symbol, chosen)
        return chosen

    @staticmethod
    def display_symbol(venue_symbol: str) -> str:
        """The venue's symbol -> the form the rest of this system uses.

        `SOL/USDT:USDT` -> `SOL/USDT`. Without this, reconciliation compares
        `SOL/USDT` from the local book against `SOL/USDT:USDT` from the venue,
        finds no match, and reports every single real position as CRITICAL
        "missing at venue" — a false alarm on exactly the alert that is supposed
        to mean a position was liquidated or closed by hand.
        """
        return venue_symbol.split(":")[0] if venue_symbol else venue_symbol

    # -- sizing ----------------------------------------------------------

    async def check_size(self, symbol: str, qty: float, price: float) -> SizingCheck:
        """Round `qty` to the venue's step and verify it clears the minimums.

        RETURNS A REFUSAL RATHER THAN A BUMPED-UP SIZE. If a position is below the
        venue's minimum notional, the honest answers are "do not trade" or "the
        operator chooses a bigger size" — silently rounding UP to the minimum would
        have the system stake more than the Risk Gateway approved, which is the one
        direction sizing must never move on its own.

        Rounding DOWN to the step is different and is done: it is the venue's own
        granularity, and the alternative is a rejected order.
        """
        resolved = await self.resolve_symbol(symbol)
        if resolved is None:
            return SizingCheck(
                ok=False, qty=qty,
                reason=f"{symbol} has no linear perpetual market on {self.id}",
            )
        mkt = (await self.markets()).get(resolved)
        if mkt is None:
            return SizingCheck(ok=False, qty=qty, reason=f"{symbol} is not a market on {self.id}")

        try:
            # RESOLVED, not the caller's symbol: ccxt rounds to the market it looks
            # up, and the spot step differs from the perpetual's (Bybit SOL: 0.001
            # vs 0.1). Rounding to the wrong step is a rejected order.
            rounded = float(self.public.amount_to_precision(resolved, qty))
        except Exception as exc:
            return SizingCheck(ok=False, qty=qty, reason=f"could not round to the venue's step: {exc}")

        limits = mkt.get("limits") or {}
        min_qty = ((limits.get("amount") or {}).get("min"))
        min_cost = ((limits.get("cost") or {}).get("min"))

        if rounded <= 0:
            return SizingCheck(
                ok=False, qty=rounded, min_qty=min_qty, min_notional=min_cost,
                reason=(
                    f"{qty:g} rounds to zero at {symbol}'s step size on {self.id}. "
                    f"The position is smaller than the venue can represent."
                ),
            )
        if min_qty is not None and rounded < float(min_qty):
            return SizingCheck(
                ok=False, qty=rounded, min_qty=min_qty, min_notional=min_cost,
                reason=f"{rounded:g} is below {symbol}'s minimum quantity of {min_qty:g} on {self.id}",
            )
        if min_cost is not None and price > 0 and rounded * price < float(min_cost):
            return SizingCheck(
                ok=False, qty=rounded, min_qty=min_qty, min_notional=min_cost,
                reason=(
                    f"notional {rounded * price:.2f} is below {symbol}'s minimum of "
                    f"{float(min_cost):.2f} on {self.id}"
                ),
            )

        return SizingCheck(ok=True, qty=rounded, min_qty=min_qty, min_notional=min_cost)

    # -- position mode ---------------------------------------------------

    async def hedge_mode(self) -> bool:
        """Is the account in hedge (dual-position) mode?

        Decides `positionSide` / `positionIdx` and whether `reduceOnly` is legal,
        so getting it wrong rejects every order. Failure to read it is reported as
        ONE-WAY, which is both the common case and the safer guess: a `reduceOnly`
        close sent to a hedge account is refused by the venue, whereas a missing
        `positionSide` on a one-way account is simply correct.
        """
        if self._hedge_mode is not None:
            return self._hedge_mode
        if not self.has_credentials():
            self._hedge_mode = False
            return False

        try:
            if self.id == "binance":
                res = await self.private.fapiPrivateGetPositionSideDual()
                self._hedge_mode = bool(res.get("dualSidePosition"))
            else:
                # Bybit reports it per position; positionIdx != 0 means hedge.
                positions = await self.private.fetch_positions(params={"category": "linear"})
                self._hedge_mode = any(
                    str((p.get("info") or {}).get("positionIdx", "0")) not in ("0", "")
                    for p in positions
                )
        except Exception as exc:
            logger.warning(
                "%s: could not read the position mode (%s). Assuming ONE-WAY, which is the "
                "common case; if this account is in hedge mode, orders will be rejected with "
                "a position-side error rather than filled wrongly.",
                self.id, exc,
            )
            self._hedge_mode = False
        return self._hedge_mode

    def _order_params(
        self, *, side: str, reduce_only: bool, hedge: bool, client_order_id: Optional[str]
    ) -> Dict[str, Any]:
        """The per-venue parameter matrix. See the module docstring for the rules.

        This is the one function where Binance and Bybit genuinely disagree, and it
        is centralised so a call site cannot get half of it right.
        """
        params: Dict[str, Any] = {}
        if client_order_id:
            # The idempotency key: a retried order must never produce a duplicate
            # fill, and the venue enforces that by rejecting a repeated id.
            params["clientOrderId" if self.id == "binance" else "orderLinkId"] = client_order_id

        if self.id == "binance":
            if hedge:
                # In hedge mode the position side IS the instruction, and Binance
                # REJECTS reduceOnly alongside it ("Parameter reduceOnly sent when
                # not required"). A close is the opposite side on the same leg.
                if reduce_only:
                    params["positionSide"] = "SHORT" if side == "buy" else "LONG"
                else:
                    params["positionSide"] = "LONG" if side == "buy" else "SHORT"
            elif reduce_only:
                params["reduceOnly"] = True
        else:  # bybit
            params["category"] = "linear"
            if hedge:
                # 1 = the long leg, 2 = the short leg. A close acts on the leg
                # OPPOSITE to the order's side.
                if reduce_only:
                    params["positionIdx"] = 2 if side == "buy" else 1
                else:
                    params["positionIdx"] = 1 if side == "buy" else 2
            else:
                params["positionIdx"] = 0
            if reduce_only:
                params["reduceOnly"] = True

        return params

    # -- leverage --------------------------------------------------------

    async def ensure_leverage(self, symbol: str, leverage: int) -> bool:
        """Tell the venue the leverage the position was sized for.

        WITHOUT THIS EVERY REAL-MONEY MARGIN FIGURE IS FICTION. The Risk Gateway
        sizes against a leverage the operator chose; the venue applies whatever was
        last set in its own UI. A position sized for 3x opened on an account left
        at 20x uses a fraction of the intended margin and liquidates far closer to
        entry than anything in this system believes.

        Called before the ENTRY only. A close must never change leverage — doing so
        while a position is open is rejected by both venues and would abort the
        close, which is the one action that must not be blocked.

        `False` means the venue did not accept it, and the caller must NOT proceed
        with an order sized against a leverage the exchange is not applying.
        """
        if not self.has_credentials():
            return False
        if self._leverage_set.get(symbol) == leverage:
            return True

        try:
            params = {"category": "linear"} if self.id == "bybit" else {}
            await self.private.set_leverage(leverage, symbol, params)
            self._leverage_set[symbol] = leverage
            return True
        except Exception as exc:
            message = str(exc)
            # "leverage not modified" is Bybit (110043) and Binance (-4046) saying
            # the value is ALREADY what we asked for. That is the desired state,
            # so treating it as a failure would abort a correctly-configured trade.
            if "not modified" in message.lower() or "110043" in message or "-4046" in message:
                self._leverage_set[symbol] = leverage
                return True
            logger.error(
                "%s: could not set %sx leverage on %s (%s). NOT placing an order sized for a "
                "leverage the venue is not applying.",
                self.id, leverage, symbol, message,
            )
            return False

    # -- orders ----------------------------------------------------------

    async def market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        expected_price: float = 0.0,
    ) -> OrderResult:
        """Place a market order. Never fabricates a fill.

        `reduce_only=True` is what makes a close a CLOSE. Without it a close is
        just an opposite-side order, and any surplus over the live position size
        opens a new position the other way — the operator asked to flatten and is
        now short. On Binance hedge mode the same intent is expressed through
        `positionSide` instead, because Binance rejects `reduceOnly` there; see
        `_order_params`.
        """
        if not self.has_credentials():
            return OrderResult(
                ok=False,
                error=(
                    f"no {self.id} API credentials configured for "
                    f"{'testnet' if self.testnet else 'mainnet'} "
                    f"({self.key_variable} / its _SECRET are empty)"
                ),
            )

        resolved = await self.resolve_symbol(symbol)
        if resolved is None:
            return OrderResult(
                ok=False, requested_qty=qty,
                error=f"{symbol} has no linear perpetual market on {self.id}",
            )

        check = await self.check_size(resolved, qty, expected_price)
        if not check.ok:
            return OrderResult(ok=False, error=check.reason, requested_qty=qty)

        hedge = await self.hedge_mode()
        params = self._order_params(
            side=side, reduce_only=reduce_only, hedge=hedge, client_order_id=client_order_id
        )

        try:
            order = await self.private.create_order(resolved, "market", side, check.qty, None, params)
        except Exception as exc:
            logger.error(
                "%s: order REJECTED — %s %s %s (reduceOnly=%s, params=%s): %s. "
                "No position changed; reporting no fill.",
                self.id, side, check.qty, resolved, reduce_only, params, exc,
            )
            return OrderResult(ok=False, error=str(exc), requested_qty=qty, adjusted_qty=check.qty)

        filled = order.get("filled")
        average = order.get("average") or order.get("price")
        return OrderResult(
            ok=True,
            order_id=str(order.get("id")) if order.get("id") is not None else None,
            filled_qty=float(filled) if filled not in (None, "") else None,
            # `None`, not 0.0, when the venue returned no usable price. A zero
            # would book the position at zero cost and show the whole notional as
            # profit; the caller checks for None and refuses to record a trade.
            average_price=float(average) if average not in (None, "", 0) else None,
            raw=order,
            requested_qty=qty,
            adjusted_qty=check.qty,
        )

    async def place_stop_loss(
        self, *, symbol: str, side: str, qty: float, stop_price: float,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """A RESTING stop at the venue, so a dead process is not an unprotected position.

        THIS IS THE GAP CLAUDE.md HAS ALWAYS NAMED. `PositionMonitorAgent` enforces
        stops by watching ticks in this process. That works while the process is
        alive and does nothing at all while it is not — a crash, a deploy or a
        restart leaves a real position open with no stop anywhere. Restoring the
        watch list narrows the window; only an order sitting at the exchange closes
        it, because the venue keeps working when we do not.

        Placed as a stop-MARKET reduce-only order. Market rather than limit
        deliberately: a stop-limit can be jumped through in a fast move and simply
        not fill, which is precisely the move it exists to protect against.

        `side` is the EXIT side — sell to close a long.
        """
        if not self.has_credentials():
            return OrderResult(
                ok=False,
                error=f"no {self.id} credentials configured ({self.key_variable})",
            )

        resolved = await self.resolve_symbol(symbol)
        if resolved is None:
            return OrderResult(
                ok=False, requested_qty=qty,
                error=f"{symbol} has no linear perpetual market on {self.id}",
            )

        check = await self.check_size(resolved, qty, stop_price)
        if not check.ok:
            return OrderResult(ok=False, error=check.reason, requested_qty=qty)

        hedge = await self.hedge_mode()
        params = self._order_params(
            side=side, reduce_only=True, hedge=hedge, client_order_id=client_order_id
        )
        try:
            price_str = self.public.price_to_precision(resolved, stop_price)
        except Exception:
            price_str = stop_price

        # `stopLossPrice` is ccxt's UNIFIED stop-loss trigger and is the right
        # parameter on BOTH venues. From ccxt 4.5's own create_order source:
        #
        #   binance  isStopLoss -> uppercaseType becomes 'STOP_MARKET' on a
        #            contract market and stopPrice = stopLossPrice. Identical to
        #            spelling those out by hand, which is what this used to do.
        #   bybit    isStopLossOrder -> DERIVES `triggerDirection` from the order
        #            side (a sell stop triggers on a fall, a buy stop on a rise)
        #            and forces reduceOnly.
        #
        # THE OLD BYBIT PATH NEVER PLACED AN ORDER AT ALL. It sent `triggerPrice`,
        # which makes ccxt treat this as a GENERIC trigger order — and a generic
        # trigger order requires an explicit direction:
        #
        #     ArgumentsRequired: bybit stop/trigger orders require a
        #     triggerDirection parameter, either "ascending" or "descending"
        #
        # raised before any request left the process. The handler below caught it
        # and logged "stop-loss order REJECTED", which reads as the venue refusing
        # a stop rather than as this process never having asked for one. It also
        # passed `stopLoss` as a bare string where ccxt expects an object, which
        # would have attached a SECOND position-level stop on top of the first.
        #
        # Both venues omit `stopLossPrice` from the outgoing request, so neither
        # sees an unknown parameter.
        order_type = "market"
        params["stopLossPrice"] = price_str
        # Trigger on the MARK price on both venues, which is what each evaluates
        # liquidation against. A last-price trigger can be moved by a thin book on
        # a single print — exactly the move a stop exists to survive. This was
        # previously set on Bybit only, leaving Binance stops on last price.
        if self.id == "bybit":
            params["triggerBy"] = "MarkPrice"
        else:
            params["workingType"] = "MARK_PRICE"

        try:
            order = await self.private.create_order(resolved, order_type, side, check.qty, None, params)
        except Exception as exc:
            logger.error(
                "%s: stop-loss order REJECTED for %s at %s (%s). The position has NO resting "
                "protection at the venue — the in-process monitor is the only stop, and it "
                "stops working if this process does.",
                self.id, symbol, stop_price, exc,
            )
            return OrderResult(ok=False, error=str(exc), requested_qty=qty, adjusted_qty=check.qty)

        return OrderResult(
            ok=True,
            order_id=str(order.get("id")) if order.get("id") is not None else None,
            raw=order,
            requested_qty=qty,
            adjusted_qty=check.qty,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel a resting order. True only when the venue confirmed it.

        A stop that is not cancelled after its position closes becomes an order to
        OPEN the opposite position the next time price touches it.
        """
        try:
            params = {"category": "linear"} if self.id == "bybit" else {}
            # Resolved, so the cancel is addressed to the market the order was
            # placed on. A cancel aimed at the spot symbol does not fail loudly —
            # it reports "unknown order", which this method treats as success, and
            # the stop would be left resting on a flat account.
            resolved = await self.resolve_symbol(symbol) or symbol
            await self.private.cancel_order(order_id, resolved, params)
            return True
        except Exception as exc:
            message = str(exc).lower()
            # Already gone is the desired end state, not a failure.
            if "unknown order" in message or "does not exist" in message or "not exists" in message:
                return True
            logger.warning("%s: could not cancel order %s on %s: %s", self.id, order_id, symbol, exc)
            return False

    async def resting_stops(self, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """The stop orders currently resting at the venue for `symbol`.

        `None` when the venue could not be asked, a list otherwise — the same
        None-is-not-empty rule `open_positions` follows, and for the same reason:
        "no stop is resting" would justify placing another one, and "we could not
        reach the venue" must never do that. Two live reduce-only stops means the
        second, after the first fires and flattens, is an order to OPEN the
        opposite position.

        LISTING THEM IS ITSELF VENUE-SPECIFIC. Binance returns STOP_MARKET orders
        from a plain `fetch_open_orders`. Bybit keeps conditional orders in a
        separate book and returns NOTHING for them unless the request asks —
        `trigger=True`, which ccxt maps to `orderFilter=StopOrder`. Asking Bybit
        the Binance way answers "no stops are resting" while a stop rests.
        """
        resolved = await self.resolve_symbol(symbol)
        if resolved is None or not self.has_credentials():
            return None
        try:
            if self.id == "bybit":
                orders = await self.private.fetch_open_orders(
                    resolved, None, None, {"category": "linear", "trigger": True}
                )
            else:
                orders = await self.private.fetch_open_orders(resolved)
        except Exception as exc:
            logger.warning("%s: could not list resting orders for %s: %s", self.id, symbol, exc)
            return None

        out: List[Dict[str, Any]] = []
        for o in orders or []:
            trigger = o.get("stopLossPrice") or o.get("triggerPrice") or o.get("stopPrice")
            if trigger in (None, "", 0):
                continue  # a plain resting limit order, not a stop
            out.append({
                "id": str(o.get("id")) if o.get("id") is not None else None,
                "side": o.get("side"),
                "qty": float(o["amount"]) if o.get("amount") else None,
                "triggerPrice": float(trigger),
                "reduceOnly": o.get("reduceOnly"),
                "type": o.get("type"),
            })
        return out

    # -- account (PRIVATE) ------------------------------------------------

    async def free_usdt(self) -> Optional[float]:
        """Free USDT collateral, or None when it cannot be read.

        None, never 0.0. "You have no money" and "we could not ask" are different
        facts, and a 0.0 here would be the denominator of every percentage the
        session reports.
        """
        if not self.has_credentials():
            return None
        try:
            params = {"category": "linear"} if self.id == "bybit" else {}
            raw = await self.private.fetch_balance(params)
        except Exception as exc:
            logger.warning("%s: could not read the balance: %s", self.id, exc)
            return None

        # Both ccxt shapes are real; which appears depends on venue and market type.
        usdt = raw.get("USDT")
        if isinstance(usdt, dict) and isinstance(usdt.get("free"), (int, float)):
            return float(usdt["free"])
        free = raw.get("free")
        if isinstance(free, dict) and isinstance(free.get("USDT"), (int, float)):
            return float(free["USDT"])
        return None

    async def open_positions(self) -> Optional[List[Dict[str, Any]]]:
        """What the VENUE says is open. None when it could not be asked.

        This is the reconciliation input, and None is load-bearing: an empty list
        means "the venue reports nothing open", which would justify dropping a
        local position. "We could not reach the venue" must never do that.
        """
        if not self.has_credentials():
            return None
        try:
            params = {"category": "linear"} if self.id == "bybit" else {}
            raw = await self.private.fetch_positions(None, params)
        except Exception as exc:
            logger.warning("%s: could not read positions: %s", self.id, exc)
            return None

        out: List[Dict[str, Any]] = []
        for p in raw or []:
            contracts = p.get("contracts")
            if contracts in (None, "", 0, 0.0):
                continue  # a flat row, which both venues return for touched symbols
            venue_symbol = p.get("symbol") or ""
            out.append({
                # The caller's form. Reconciliation keys on this, and the local
                # book holds "SOL/USDT" while the venue reports "SOL/USDT:USDT".
                "symbol": self.display_symbol(venue_symbol),
                "venueSymbol": venue_symbol,
                "side": p.get("side"),
                "qty": abs(float(contracts)),
                "entryPrice": float(p["entryPrice"]) if p.get("entryPrice") else None,
                "leverage": float(p["leverage"]) if p.get("leverage") else None,
                "unrealizedPnl": float(p["unrealizedPnl"]) if p.get("unrealizedPnl") is not None else None,
                "liquidationPrice": float(p["liquidationPrice"]) if p.get("liquidationPrice") else None,
            })
        return out

    async def close(self) -> None:
        for client in (self.public, self.private):
            try:
                await client.close()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
#
# One instance, so `load_markets` is paid for once and the leverage/position-mode
# caches are shared. `reset_venue()` exists for tests and for the settings page's
# venue switch — switching exchange MUST build a new client, because the old one
# holds the other venue's markets, credentials and cached position mode.

_venue: Optional[Venue] = None


def get_venue() -> Venue:
    global _venue
    if _venue is None:
        _venue = Venue()
    return _venue


def reset_venue() -> None:
    """Drop the cached venue. The next `get_venue()` re-reads the environment."""
    global _venue
    _venue = None
