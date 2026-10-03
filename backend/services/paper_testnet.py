"""Mirror PAPER trades onto Bybit's testnet, so a paper result is a real fill.

WHAT THIS IS FOR
================
The operator's ask, in their words: *"if i enable that button the trade is extra
add to api via testnet to execute trade in testing bybit server account ... so
that i confidently check with real trade"*.

A simulated fill is booked at the last observed price, instantly, in full. That
is honest bookkeeping and it is also the most flattering possible execution: no
spread crossed, no slippage, no partial fill, no minimum size, no leverage
rejection, no rate limit, no venue outage. Every one of those is a real cost that
shows up on day one of real money and on none of the paper days before it.

With this on, a paper entry places a REAL market order on Bybit's testnet and the
paper book is credited with the price the exchange actually returned. The trade
is still paper — the P&L, the equity, the win rate and every panel read the same
local book they always did — but the FILL is no longer a model of one.

WHY BYBIT TESTNET SPECIFICALLY, AND NOT A CHOICE
================================================
`scripts/bybit_testnet_roundtrip.py` already exists and already found three
real-money bugs no offline test could (the spot-vs-perpetual symbol resolution,
the Bybit stop that never reached the venue, and reconciliation comparing two
spellings of one position). Binance's futures testnet is NOT an option: ccxt
dropped support, which is exactly why this project's Binance order path remains
unverified. So this hardcodes bybit and `testnet=True` rather than reading
`EXCHANGE_ID` — a mirror that could be pointed at mainnet by a config typo is not
a testing aid, it is a way to lose money by accident.

THE FOUR SAFETY PROPERTIES, EACH ENFORCED RATHER THAN DOCUMENTED
================================================================
1. IT IS ONLY REACHABLE FROM THE SIMULATION BRANCH. `ExecutionAgent` consults it
   inside `if self.simulation_mode:`, which is false whenever `LIVE_TRADING` is
   on. Live trading and this cannot both be routing an order.
2. IT NEVER TOUCHES MAINNET. `Venue(..., testnet=True)` reads
   `BYBIT_TESTNET_API_KEY` / `_SECRET`, and `_credentials` never reads a mainnet
   variable in sandbox mode. A mainnet key pasted there is REFUSED by the sandbox
   endpoint — it fails closed.
3. IT NEVER BLOCKS A PAPER TRADE. A testnet outage, a rejected size, an expired
   key: the fill falls back to the simulated one and the trade still books. The
   alternative is a paper account that stops working because a test venue is
   down, which is a worse failure than a less faithful fill.
4. THE FALLBACK IS VISIBLE, NEVER SILENT. `trades.exchange_order_id` is the
   discriminator the rest of this system uses — NULL means no venue order stood
   behind this row. A mirrored fill carries the testnet order id; a fallback
   carries None, and the log says which and why.

   THIS WAS NOT TRUE WHEN IT WAS FIRST WRITTEN, AND SAYING SO IS THE POINT.
   `execution_agent` mints a `uuid.uuid4()` as `order_id` at the top of
   `_execute_tar` and only REPLACES it when a venue returns its own — so the
   column was non-NULL on every row, simulated or not, while this docstring,
   CLAUDE.md and the Settings panel all told the operator it discriminated. It
   is the field you reach for to answer "was this paper result real?", so a
   documented discriminator that does not discriminate is worse than an
   undocumented one. `_execute_tar` now tracks `venue_backed` and writes the
   column only when a venue order genuinely stood behind the fill;
   `tests/test_audit_findings.py` pins it.

READ AT CALL TIME, like every other operator toggle here. A module-level
`os.getenv` would be the `simulation_mode` bug again: the operator flips the
switch, is told it worked, and the running agent keeps the old behaviour until a
restart.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ENV_VAR = "PAPER_TESTNET_MIRROR"

# WHICH SANDBOX. Two are supported and the operator picks one; the default is
# Binance because that is where this account's testnet funds are.
#
# PROPERTY 2 ("it can never touch mainnet") STILL HOLDS FOR BOTH, but it is
# enforced differently in each and the difference is the whole reason
# `services/binance_testnet` exists as a separate module:
#
#   bybit    ccxt `Venue("bybit", testnet=True)`. `_credentials(testnet=True)`
#            reads BYBIT_TESTNET_* and mainnet never reads them.
#   binance  NOT ccxt. `set_sandbox_mode` is hard-refused for binanceusdm, and
#            overriding `urls['api']` by hand LOOKS like it works and silently
#            sends signed requests to MAINNET — measured: `fetch_balance`
#            dialled `https://api.binance.com/sapi/v1/capital/config/getall`,
#            a host no `fapi*` override touches. So that module hardcodes ONE
#            base url with no mainnet sibling to fall back to.
#
# THIS VARIABLE SELECTS A SANDBOX, NEVER A LIVE VENUE. Neither branch can reach
# mainnet, so an unrecognised value falls back to the default rather than
# failing closed into nothing — a typo must not silently disable the mirror the
# operator believes is on.
VENUE_CHOICE_VAR = "PAPER_TESTNET_VENUE"
DEFAULT_VENUE = "binance"
SUPPORTED_VENUES = ("binance", "bybit")

# Kept for the Bybit branch and for every existing test that names it.
VENUE_ID = "bybit"


def venue_choice() -> str:
    """Which sandbox the mirror routes to. Read at CALL time, like every toggle."""
    raw = (os.getenv(VENUE_CHOICE_VAR) or "").strip().lower()
    if raw in SUPPORTED_VENUES:
        return raw
    if raw:
        logger.warning(
            "%s=%r is not one of %s — falling back to %s. A typo must not quietly "
            "turn the mirror off while the panel still reports it on.",
            VENUE_CHOICE_VAR, raw, SUPPORTED_VENUES, DEFAULT_VENUE,
        )
    return DEFAULT_VENUE

_venue: Optional[Any] = None
# The last verification result, so the Settings panel can show something other
# than "unknown" without re-hitting the venue on every poll.
_last_check: Dict[str, Any] = {}


def enabled() -> bool:
    """Whether paper fills should be mirrored to the testnet. Read at CALL time."""
    return (os.getenv(ENV_VAR) or "").strip().lower() == "true"


def credentials_present() -> bool:
    """Whether the testnet key pair is configured at all.

    Checked separately from `enabled()` so the panel can offer the toggle and
    explain what is missing, rather than letting it be switched on into a state
    where every order fails.
    """
    if venue_choice() == "binance":
        from backend.services import binance_testnet

        return binance_testnet.credentials_present()

    from backend.services.venue import key_variable

    key = os.getenv(key_variable(VENUE_ID, testnet=True))
    secret = os.getenv(f"{VENUE_ID.upper()}_TESTNET_SECRET")
    return bool((key or "").strip() and (secret or "").strip())


def get_venue():
    """The testnet client, built once.

    Cached because building it loads the venue's whole market map, and this is
    consulted on every paper fill. `reset()` drops it so a credential change is
    picked up without a restart.
    """
    global _venue
    if _venue is None:
        if venue_choice() == "binance":
            # A FACADE, NOT A ccxt CLIENT. `Venue.__init__` builds one, and ccxt
            # is precisely what cannot reach Binance futures testnet — see
            # `services/binance_testnet`'s docstring for the measured reason.
            from backend.services.binance_testnet import BinanceTestnetVenue

            _venue = BinanceTestnetVenue()
            logger.info(
                "Paper-testnet mirror is routing to BINANCE futures testnet "
                "(direct REST, hardcoded sandbox host — ccxt cannot reach it)."
            )
            return _venue

        from backend.services.venue import Venue

        _venue = Venue(VENUE_ID, testnet=True)
        logger.info(
            "Paper-testnet mirror built a %s client in SANDBOX mode (key variable %s).",
            VENUE_ID, _venue.key_variable,
        )
    return _venue


def reset() -> None:
    """Drop the cached client. For tests, and after a credential change."""
    global _venue
    _venue = None


def active() -> bool:
    """Enabled AND usable. This is what the execution path asks.

    Both halves matter: enabled-without-credentials must not send an order that
    is certain to be refused, and it must not silently look like it is working.
    """
    if not enabled():
        return False
    if not credentials_present():
        choice = venue_choice()
        logger.warning(
            "%s is on but no %s testnet credentials are set (%s_TESTNET_API_KEY / "
            "%s_TESTNET_SECRET). Paper fills stay simulated.",
            ENV_VAR, choice, choice.upper(), choice.upper(),
        )
        return False
    return True


async def verify() -> Dict[str, Any]:
    """Prove the credentials work by making a REAL authenticated call.

    A key that merely EXISTS proves nothing — it can be revoked, be a mainnet key
    pasted into the testnet slot, or lack trade permissions, and every one of
    those looks identical until an order is refused. So this fetches the balance,
    which is the cheapest call that requires a valid signature.

    Never raises: the Settings panel calls it, and a failed check is information,
    not an error page.
    """
    global _last_check
    import time

    if not credentials_present():
        _last_check = {
            "ok": False,
            "checkedAt": time.time(),
            "reason": (
                f"No testnet credentials. Set {venue_choice().upper()}_TESTNET_API_KEY and "
                f"{venue_choice().upper()}_TESTNET_SECRET in .env — they are deliberately SEPARATE from "
                "the mainnet pair so verifying never requires pasting a testnet key "
                "over a live one."
            ),
        }
        return _last_check

    if venue_choice() == "binance":
        # Its own verify(), because it has one more thing to report: the
        # machine's clock offset. A 33-second skew makes every signed request
        # fail -1021, which reads as a bad key and sends the operator to
        # regenerate credentials that were never the problem.
        from backend.services import binance_testnet

        _last_check = {**(await binance_testnet.verify()), "checkedAt": time.time(),
                       "venue": "binance"}
        return _last_check

    try:
        venue = get_venue()
        balance = await venue.free_usdt()
        if balance is None:
            _last_check = {
                "ok": False,
                "checkedAt": time.time(),
                "reason": (
                    "The testnet answered but returned no balance. The key may lack "
                    "read permission, or it may be a MAINNET key in the testnet slot — "
                    "a sandbox endpoint refuses one, which is the fail-closed behaviour "
                    "`_credentials` is built around."
                ),
            }
        else:
            _last_check = {
                "ok": True,
                "checkedAt": time.time(),
                "balanceUsdt": float(balance),
                "reason": (
                    f"Authenticated against {VENUE_ID} testnet. Free balance "
                    f"{float(balance):,.2f} USDT. Paper entries will place real orders "
                    f"there and book the price the exchange returns."
                ),
            }
    except Exception as exc:  # noqa: BLE001 — a failed check is information
        _last_check = {
            "ok": False,
            "checkedAt": time.time(),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return _last_check


def status() -> Dict[str, Any]:
    """For `GET /api/admin/testnet` and the Settings panel. Never returns a key."""
    from backend.core.config import settings

    return {
        "enabled": enabled(),
        "credentialsPresent": credentials_present(),
        "venue": venue_choice(),
        "supportedVenues": list(SUPPORTED_VENUES),
        "keyVariable": f"{venue_choice().upper()}_TESTNET_API_KEY",
        "secretVariable": f"{venue_choice().upper()}_TESTNET_SECRET",
        # Surfaced because the mirror is unreachable while live trading is on —
        # the execution agent only consults it inside its simulation branch — and
        # an operator seeing the toggle on while nothing mirrors deserves to know
        # why without reading the source.
        "liveTradingOn": bool(settings.LIVE_TRADING),
        "reachable": enabled() and credentials_present() and not settings.LIVE_TRADING,
        "lastCheck": dict(_last_check) if _last_check else None,
        "note": (
            f"Paper trades are mirrored onto {venue_choice()} demo/testnet: the order is real, the "
            "fill price is the exchange's, and the P&L is still paper. A testnet "
            "failure falls back to a simulated fill rather than blocking the trade, "
            "and the trade row records which happened — `exchange_order_id` is set "
            "for a mirrored fill and NULL for a simulated one."
        ),
    }


async def place(
    *,
    symbol: str,
    side: str,
    qty: float,
    leverage: Optional[int] = None,
    reduce_only: bool = False,
    client_order_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Place one real order on the testnet. Returns the fill, or None.

    None means "use the simulated fill" and is returned for every failure — an
    unusable venue, a refused size, a leverage the account cannot set. Property 3
    in the module docstring: a test venue being down must not stop paper trading.

    The leverage call is NOT fatal here, unlike the live path where a refusal
    ABORTS the trade. On mainnet, filling at a leverage we know is wrong is
    trading on a false number; on testnet the position is play money and a
    less-faithful mirror still beats no mirror. That asymmetry is deliberate and
    is the only place this module is more permissive than the real one.
    """
    if not active():
        return None

    try:
        venue = get_venue()
        if leverage:
            try:
                await venue.ensure_leverage(symbol, int(leverage))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Testnet mirror could not set %sx leverage on %s (%s). Continuing — "
                    "the fill is still a real one, at whatever leverage the testnet "
                    "account holds.",
                    leverage, symbol, exc,
                )

        result = await venue.market_order(
            symbol=symbol,
            side=side,
            qty=qty,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
        )
        if not getattr(result, "ok", False):
            logger.warning(
                "Testnet mirror REFUSED %s %s %s: %s. Falling back to a simulated fill; "
                "the trade row will carry no exchange_order_id.",
                side, qty, symbol, getattr(result, "error", "no reason given"),
            )
            return None

        price = getattr(result, "average_price", None)
        filled = getattr(result, "filled_qty", None)
        if not price or price <= 0:
            # A venue that accepted the order but reported no price cannot tell us
            # what it cost. Booking the simulated price against a real order id
            # would attach a made-up fill to a real trade, which is worse than
            # either alone (invariant 6).
            logger.warning(
                "Testnet mirror filled %s but reported no average price. Falling back "
                "to the simulated fill rather than pairing a real order id with a "
                "modelled price.",
                symbol,
            )
            return None

        # AN UNFILLED ORDER IS NOT A FILL, AND `filled or qty` SAID IT WAS.
        #
        # `float(filled) if filled else float(qty)` treats 0.0 and None as
        # falsy and substitutes the REQUESTED quantity — so a testnet order that
        # was accepted and filled nothing came back as a complete fill of
        # everything asked for. That is precisely the flattering execution this
        # whole module exists to remove: the operator turns the mirror on to see
        # real slippage, partial fills and refused sizes, and the one case that
        # is neither a fill nor a refusal was rounded up into a perfect fill.
        #
        # Both cases fall back to the simulated fill instead, because a fallback
        # is honest and recoverable while a fabricated quantity is neither. The
        # paper book then holds a modelled position rather than a claimed real
        # one, and the log says which.
        if filled is None:
            logger.warning(
                "Testnet mirror accepted %s %s but reported NO filled quantity. Falling back "
                "to the simulated fill rather than assuming the whole size traded.",
                side, symbol,
            )
            return None
        filled = float(filled)
        if filled <= 0:
            logger.warning(
                "Testnet mirror accepted %s %s and filled NOTHING (0 of %s). Falling back to "
                "the simulated fill — an unfilled order is not a fill, and booking one would "
                "put a position in the paper book that does not exist at the testnet.",
                side, symbol, qty,
            )
            return None

        logger.info(
            "Testnet mirror FILLED %s %s %s at %s (order %s).",
            side, filled, symbol, price, getattr(result, "order_id", None),
        )
        return {
            "order_id": getattr(result, "order_id", None),
            "price": float(price),
            "filled_qty": filled,
        }
    except Exception as exc:  # noqa: BLE001 — never break a paper trade
        logger.warning(
            "Testnet mirror failed for %s %s %s (%s). Falling back to a simulated fill.",
            side, qty, symbol, exc,
        )
        return None
