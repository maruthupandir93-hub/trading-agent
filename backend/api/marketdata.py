"""Market Data API (`/api/marketdata`) — every third-party call the dashboard needs.

WHY THIS ROUTER EXISTS
----------------------
The Next.js layer used to call Binance, Yahoo and alternative.me directly from
its own route handlers. Deployed on Vercel, those handlers run in whatever region
Vercel chooses, and from a US region Binance answers 451 — "Unavailable For Legal
Reasons". The dashboard showed 502s on /api/candles, /api/orderflow and
/api/quote while every local route worked fine.

No amount of retrying, keying or header-setting fixes a 451. The request has to
COME FROM a region the upstream serves. This backend runs on a machine in one, so
the calls live here and the Next routes proxy to it server-to-server, where no
browser rules apply.

    Browser --https--> Vercel route --http--> THIS --> Binance / Yahoo / ...

SHAPES ARE COPIED FROM THE ROUTES THIS REPLACES, EXACTLY
--------------------------------------------------------
Each response below reproduces what its Next.js counterpart already returned —
same field names, same types, same nulls. That is not laziness: `lib/indicators`,
`lib/orderFlow`, `lib/eventDetection` and the chart components all parse these,
and a "cleaner" shape here would mean rewriting them too, turning a networking
fix into a data-model migration. The proxy routes therefore pass bodies through
untouched.

WHY NOT REUSE `/api/market/klines`
----------------------------------
It exists and it looks like the same thing. It is not: it goes through ccxt with
`defaultType=future`, so it returns PERPETUAL FUTURES candles, while
`/api/candles` has always served Binance SPOT. Pointing the frontend at it would
silently swap one market for another — same symbol, different prices, no error.
`/api/market/*` stays the agent's normalized view; this is the dashboard's.

EVERY ROUTE HERE IS READ-ONLY. Nothing takes a write, and nothing touches the
execution path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from fastapi import APIRouter, HTTPException, Query

from backend.services.ticker_stream import get_ticker_stream
from backend.services.upstream import UpstreamResult, fetch_json, fetch_text

logger = logging.getLogger(__name__)

router = APIRouter()

BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
YAHOO = "https://query1.finance.yahoo.com"
FEAR_GREED = "https://api.alternative.me"

# Mirrors lib/candleSource.server.ts's BINANCE_INTERVALS exactly. Kept as its own
# constant rather than validated loosely, so an unsupported interval is a 400
# from us instead of a confusing empty array from Binance.
BINANCE_INTERVALS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d", "1w"})

BINANCE_INTERVAL_MS = {
    "1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000, "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000, "1d": 24 * 60 * 60_000, "1w": 7 * 24 * 60 * 60_000,
}

# Yahoo's intraday granularity is more restricted than Binance's, and it rejects
# a long range at a fine granularity. Copied from YAHOO_INTERVAL_MAP in
# lib/candleSource.server.ts — this IS the honest ceiling on equity history.
YAHOO_INTERVAL_MAP = {
    "1m": ("1m", "5d"), "5m": ("5m", "1mo"), "15m": ("15m", "1mo"),
    "1h": ("60m", "3mo"), "4h": ("60m", "3mo"), "1d": ("1d", "1y"),
    "1w": ("1wk", "5y"),
}

BINANCE_MAX_PER_CALL = 1000
MAX_BINANCE_PAGES = 10  # 10,000 bars ceiling per deep fetch, same as the TS version

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")


def _clean_symbol(symbol: str, *, field: str = "symbol") -> str:
    """Reject anything that is not a plausible ticker.

    These values are interpolated into upstream URLs. Validating here means a
    malformed symbol is a 400 from this service rather than an odd request sent
    to Binance on the caller's behalf.
    """
    if not symbol or not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail=f"{field} {symbol!r} is not a valid ticker")
    return symbol


def _fail(result: UpstreamResult, what: str) -> None:
    """Turn a failed upstream call into an HTTP error that names the cause.

    A geo-block is 502 with the region called out explicitly. It used to reach
    the browser as a bare 502 with no body, which is how "Binance blocks this
    region" spent so long looking like "the candles endpoint is broken".
    """
    detail = result.error or f"{what}: upstream returned nothing"
    if result.geo_blocked:
        detail = (
            f"{detail} — THIS BACKEND'S REGION IS BLOCKED BY THE PROVIDER. Moving the call "
            f"here fixed Vercel's region, not this one. Check the host's location."
        )
    raise HTTPException(status_code=502, detail=detail)


# ---------------------------------------------------------------------------
# Candles — replaces app/api/candles/route.ts
# ---------------------------------------------------------------------------


def _map_binance_klines(rows: Any) -> List[Dict[str, float]]:
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        try:
            out.append({
                "t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                "l": float(r[3]), "c": float(r[4]), "v": float(r[5]),
            })
        except (IndexError, TypeError, ValueError):
            # One malformed row must not void the whole series.
            continue
    return out


async def _binance_candles(symbol: str, interval: str, limit: int) -> List[Dict[str, float]]:
    url = f"{BINANCE_SPOT}/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    result = await fetch_json(url, label="Binance klines")
    if not result.ok:
        _fail(result, "Binance klines")
    return _map_binance_klines(result.data)


async def _yahoo_candles(symbol: str, interval: str) -> List[Dict[str, float]]:
    mapped = YAHOO_INTERVAL_MAP.get(interval)
    if mapped is None:
        raise HTTPException(status_code=400, detail=f"Unsupported interval for equities: {interval}")
    y_interval, y_range = mapped
    url = f"{YAHOO}/v8/finance/chart/{symbol}?interval={y_interval}&range={y_range}"
    result = await fetch_json(url, label="Yahoo chart")
    if not result.ok:
        _fail(result, "Yahoo chart")

    chart = (result.data or {}).get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise HTTPException(status_code=502, detail="Yahoo chart returned no result for this symbol")

    first = results[0]
    timestamps = first.get("timestamp") or []
    quote = ((first.get("indicators") or {}).get("quote") or [{}])[0]

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    def at(series: List[Any], i: int) -> Any:
        return series[i] if i < len(series) else None

    candles: List[Dict[str, float]] = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = at(opens, i), at(highs, i), at(lows, i), at(closes, i)
        if any(x is None for x in (o, h, l, c)):
            # Yahoo emits null OHLC for halted and pre-market slots. Skipped
            # rather than zero-filled — a zero candle draws a wick to the axis
            # and reads as a crash that never happened.
            continue
        v = at(volumes, i)
        candles.append({"t": int(ts) * 1000, "o": float(o), "h": float(h),
                        "l": float(l), "c": float(c), "v": float(v or 0)})
    return candles


@router.get("/candles")
async def get_candles(
    symbol: str = Query(..., description="Binance slug for crypto, ticker for equity"),
    type: str = Query(..., description="'crypto' or 'equity'"),
    interval: str = Query("1h"),
    limit: int = Query(200, ge=20, le=1000),
) -> Dict[str, Any]:
    """OHLC history. Response shape matches app/api/candles/route.ts exactly."""
    symbol = _clean_symbol(symbol)

    if type == "crypto":
        if interval not in BINANCE_INTERVALS:
            raise HTTPException(status_code=400, detail=f"Unsupported interval for crypto: {interval}")
        candles = await _binance_candles(symbol, interval, limit)
    elif type == "equity":
        candles = await _yahoo_candles(symbol, interval)
        candles = candles[-limit:]
    else:
        raise HTTPException(status_code=400, detail='type must be "crypto" or "equity"')

    return {"symbol": symbol, "interval": interval, "candles": candles, "limit": limit}


@router.get("/candles/deep")
async def get_candles_deep(
    symbol: str = Query(...),
    type: str = Query(...),
    interval: str = Query("1h"),
    bars: int = Query(1000, ge=20, le=BINANCE_MAX_PER_CALL * MAX_BINANCE_PAGES),
) -> Dict[str, Any]:
    """Deep history for backtests — replaces lib/candleSource.server.fetchDeepHistory.

    Binance caps one klines call at 1000 bars, so this walks backwards with
    `endTime`, stitching pages. Real pagination, not a bigger single request
    pretending the cap does not exist.
    """
    symbol = _clean_symbol(symbol)

    if type == "equity":
        all_candles = await _yahoo_candles(symbol, interval)
        candles = all_candles[-bars:]
        note = (
            f"Yahoo returned {len(all_candles)} bars total at this granularity "
            f"(fewer than the {bars} requested) — equities have a shorter available history "
            f"at fine granularity than crypto."
            if len(all_candles) < bars
            else f"Yahoo, {len(candles)} of {len(all_candles)} available bars used."
        )
        return {"symbol": symbol, "interval": interval, "candles": candles, "sourceNote": note}

    if type != "crypto":
        raise HTTPException(status_code=400, detail='type must be "crypto" or "equity"')
    if interval not in BINANCE_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Unsupported interval for crypto: {interval}")

    target = min(bars, BINANCE_MAX_PER_CALL * MAX_BINANCE_PAGES)
    end_time: Optional[int] = None
    pages: List[List[Dict[str, float]]] = []
    collected = 0

    for _ in range(MAX_BINANCE_PAGES):
        if collected >= target:
            break
        page_limit = min(BINANCE_MAX_PER_CALL, target - collected)
        url = (f"{BINANCE_SPOT}/api/v3/klines?symbol={symbol.upper()}"
               f"&interval={interval}&limit={page_limit}")
        if end_time is not None:
            url += f"&endTime={end_time}"

        result = await fetch_json(url, label="Binance klines (deep)")
        if not result.ok:
            if pages:
                # Partial history is genuinely useful for a backtest, and a hard
                # failure here would throw away pages already fetched. The
                # shortfall is reported in sourceNote instead.
                logger.warning("Deep fetch stopped early: %s", result.error)
                break
            _fail(result, "Binance klines (deep)")

        rows = result.data if isinstance(result.data, list) else []
        if not rows:
            break
        page = _map_binance_klines(rows)
        if not page:
            break
        pages.insert(0, page)
        collected += len(page)
        end_time = page[0]["t"] - 1
        if len(rows) < page_limit:
            break  # upstream ran out of history

    seen = set()
    merged: List[Dict[str, float]] = []
    for page in pages:
        for candle in page:
            if candle["t"] in seen:
                continue
            seen.add(candle["t"])
            merged.append(candle)
    merged.sort(key=lambda c: c["t"])

    note = (
        f"Binance returned {len(merged)} bars (fewer than the {bars} requested — "
        f"that's all the history available at this granularity)."
        if len(merged) < bars
        else f"Binance, {len(merged)} bars."
    )
    return {"symbol": symbol, "interval": interval, "candles": merged, "sourceNote": note}


# ---------------------------------------------------------------------------
# Order flow — replaces app/api/orderflow/route.ts
# ---------------------------------------------------------------------------


@router.get("/orderflow")
async def get_orderflow(binance: str = Query(..., description="Binance slug, e.g. BTCUSDT")) -> Dict[str, Any]:
    """Order book depth and the recent trade tape. Crypto only."""
    symbol = _clean_symbol(binance, field="binance").upper()

    depth_result, trades_result = await asyncio.gather(
        fetch_json(f"{BINANCE_SPOT}/api/v3/depth?symbol={symbol}&limit=50", label="Binance depth"),
        fetch_json(f"{BINANCE_SPOT}/api/v3/aggTrades?symbol={symbol}&limit=200", label="Binance aggTrades"),
    )

    if not depth_result.ok:
        _fail(depth_result, "Binance depth")
    if not trades_result.ok:
        _fail(trades_result, "Binance aggTrades")

    depth = depth_result.data or {}
    trades = trades_result.data or []

    def levels(rows: Any) -> List[Dict[str, float]]:
        out = []
        for row in rows or []:
            try:
                out.append({"price": float(row[0]), "qty": float(row[1])})
            except (IndexError, TypeError, ValueError):
                continue
        return out

    tape = []
    for t in trades if isinstance(trades, list) else []:
        try:
            tape.append({
                "price": float(t["p"]), "qty": float(t["q"]),
                "time": int(t["T"]),
                # True means the buyer was the maker, i.e. the SELLER was the
                # aggressor. Passed through with the same name the frontend
                # already reads.
                "buyerIsMaker": bool(t["m"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    return {"bids": levels(depth.get("bids")), "asks": levels(depth.get("asks")), "trades": tape}


# ---------------------------------------------------------------------------
# Quotes — replaces app/api/quote/route.ts
# ---------------------------------------------------------------------------


@router.get("/quote")
async def get_quotes(symbols: str = Query("", description="Comma-separated equity tickers")) -> Dict[str, Any]:
    """Equity quotes from Yahoo's CHART endpoint, one call per symbol.

    NOT `/v7/finance/quote`, WHICH IS DEAD — AND THIS IS A SECOND, SEPARATE BUG
    ---------------------------------------------------------------------------
    The route this replaces called `/v7/finance/quote?symbols=A,B`, batching every
    ticker into one request. That endpoint now answers:

        401 {"finance":{"error":{"code":"Unauthorized",
             "description":"User is unable to access this feature"}}}

    Yahoo closed it to unauthenticated callers — it needs a crumb+cookie pair
    now. So `/api/quote` was failing for a reason that has NOTHING to do with the
    Vercel geo-block that broke candles and order flow. Both surfaced as the same
    502 in the dashboard, which is exactly why it looked like one fault: moving
    the call to this backend would have fixed the other two and left this one
    broken, still returning 502, still looking like a region problem.

    `/v8/finance/chart` is keyless and still open — it is the same endpoint the
    candle route already uses successfully — and its `meta` block carries both
    numbers we need.

    THE COST, STATED: v8 is one request PER SYMBOL, where v7 took a batch. For a
    watchlist of a few equities that is fine, and they are issued concurrently.
    It would not be fine for hundreds, and the honest fix at that scale is a real
    quote provider, not a bigger fan-out.
    """
    wanted: List[str] = []
    for raw in symbols.split(","):
        s = raw.strip().upper()
        if s and s not in wanted:
            wanted.append(_clean_symbol(s, field="symbols"))

    if not wanted:
        return {"quotes": []}

    async def one(sym: str) -> Dict[str, Any]:
        url = f"{YAHOO}/v8/finance/chart/{sym}?interval=1d&range=5d"
        result = await fetch_json(url, label=f"Yahoo chart ({sym})", retries=0)
        if not result.ok:
            # Per-symbol failure, not a whole-request failure. One delisted or
            # mistyped ticker must not blank the rest of the watchlist.
            return {"symbol": sym, "price": None, "prevClose": None, "error": result.error}

        results = ((result.data or {}).get("chart") or {}).get("result") or []
        meta = (results[0].get("meta") or {}) if results else {}
        price = _num(meta.get("regularMarketPrice"))
        # `chartPreviousClose` is the field v8 actually populates;
        # `previousClose` is present but null on every response checked.
        prev = _num(meta.get("chartPreviousClose"))
        if prev is None:
            prev = _num(meta.get("previousClose"))

        return {
            "symbol": sym,
            "price": price,
            "prevClose": prev,
            "error": None if price is not None else "Yahoo answered but carried no price for this symbol",
        }

    quotes = await asyncio.gather(*(one(s) for s in wanted))
    priced = [q for q in quotes if q["price"] is not None]

    # A total failure is a 502 — every symbol failing is a provider problem, and
    # returning 200 with a list of nulls would let the UI render an all-blank
    # grid as though the market had no prices.
    if wanted and not priced:
        first_error = next((q["error"] for q in quotes if q["error"]), "no symbol returned a price")
        raise HTTPException(status_code=502, detail=f"Yahoo quote: {first_error}")

    return {"quotes": list(quotes), "availableCount": len(priced), "totalCount": len(wanted)}


# ---------------------------------------------------------------------------
# Market intelligence — replaces app/api/marketintel/route.ts
# ---------------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    """Parse to float, or None. Never 0.0 as a stand-in for a failed parse.

    A funding rate of 0.0 and a funding rate we could not read are different
    facts, and this codebase has been bitten enough times by the second wearing
    the first's clothes (`fng: 50` on a failed fetch, `prob_of_ruin: 0.0` with
    no data) that the distinction is preserved everywhere.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/marketintel")
async def get_market_intel(binance: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Fear & Greed plus per-symbol derivatives context.

    Every sub-fetch degrades independently — Fear & Greed still answers when the
    derivatives endpoints do not, and vice versa. `available` flags say which
    half is real rather than presenting a partial answer as a whole one.
    """
    symbol = _clean_symbol(binance, field="binance").upper() if binance else None

    async def maybe(url: str, label: str) -> UpstreamResult:
        if symbol is None:
            return UpstreamResult(data=None, error="no symbol requested")
        return await fetch_json(url, label=label)

    fng_result, premium, oi, top, taker = await asyncio.gather(
        fetch_json(f"{FEAR_GREED}/fng/?limit=2", label="Fear & Greed"),
        maybe(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex?symbol={symbol}", "Binance premiumIndex"),
        maybe(f"{BINANCE_FUTURES}/fapi/v1/openInterest?symbol={symbol}", "Binance openInterest"),
        maybe(f"{BINANCE_FUTURES}/futures/data/topLongShortAccountRatio?symbol={symbol}&period=5m&limit=1",
              "Binance topLongShortAccountRatio"),
        maybe(f"{BINANCE_FUTURES}/futures/data/takerlongshortRatio?symbol={symbol}&period=5m&limit=1",
              "Binance takerlongshortRatio"),
    )

    fear_greed = None
    if fng_result.ok:
        points = []
        for d in (fng_result.data or {}).get("data") or []:
            value = _num(d.get("value"))
            ts = _num(d.get("timestamp"))
            if value is None:
                continue
            points.append({
                "value": int(value),
                "classification": d.get("value_classification"),
                "timestamp": int(ts * 1000) if ts is not None else None,
            })
        if points:
            fear_greed = {"current": points[0], "previous": points[1] if len(points) > 1 else None}

    def first_row(result: UpstreamResult) -> Dict[str, Any]:
        if result.ok and isinstance(result.data, list) and result.data:
            return result.data[0] or {}
        return {}

    premium_data = premium.data if premium.ok and isinstance(premium.data, dict) else {}
    oi_data = oi.data if oi.ok and isinstance(oi.data, dict) else {}
    top_row = first_row(top)
    taker_row = first_row(taker)

    long_account = _num(top_row.get("longAccount"))

    derivatives = None
    if symbol is not None:
        derivatives = {
            "fundingRate": _num(premium_data.get("lastFundingRate")),
            "markPrice": _num(premium_data.get("markPrice")),
            "openInterest": _num(oi_data.get("openInterest")),
            "topTraderLongShortRatio": _num(top_row.get("longShortRatio")),
            "topTraderLongAccountPct": long_account * 100 if long_account is not None else None,
            "takerBuySellRatio": _num(taker_row.get("buySellRatio")),
        }

    return {
        "fearGreed": fear_greed,
        "derivatives": derivatives,
        "fearGreedAvailable": fear_greed is not None,
        "derivativesAvailable": bool(derivatives and any(v is not None for v in derivatives.values())),
    }


# ---------------------------------------------------------------------------
# Event data — replaces app/api/eventdata/route.ts
# ---------------------------------------------------------------------------


@router.get("/eventdata")
async def get_event_data(binance: str = Query(...)) -> Dict[str, Any]:
    """Funding-rate and open-interest HISTORY, for the spike detectors."""
    symbol = _clean_symbol(binance, field="binance").upper()

    funding_result, oi_result = await asyncio.gather(
        fetch_json(f"{BINANCE_FUTURES}/fapi/v1/fundingRate?symbol={symbol}&limit=30",
                   label="Binance fundingRate"),
        fetch_json(f"{BINANCE_FUTURES}/futures/data/openInterestHist?symbol={symbol}&period=5m&limit=30",
                   label="Binance openInterestHist"),
    )

    funding_history = []
    if funding_result.ok and isinstance(funding_result.data, list):
        for f in funding_result.data:
            rate = _num(f.get("fundingRate"))
            time_ms = _num(f.get("fundingTime"))
            if rate is not None and time_ms is not None:
                funding_history.append({"rate": rate, "time": int(time_ms)})

    oi_history = []
    if oi_result.ok and isinstance(oi_result.data, list):
        for o in oi_result.data:
            value = _num(o.get("sumOpenInterest"))
            time_ms = _num(o.get("timestamp"))
            if value is not None and time_ms is not None:
                oi_history.append({"oi": value, "time": int(time_ms)})

    return {
        "fundingHistory": funding_history,
        "oiHistory": oi_history,
        # Honest partial-failure surfacing: an empty array from a failed fetch
        # is indistinguishable from an empty array meaning "no spike", so the
        # difference is stated rather than inferred.
        "fundingHistoryAvailable": bool(funding_history),
        "oiHistoryAvailable": bool(oi_history),
    }


# ---------------------------------------------------------------------------
# Live ticks — replaces the browser's direct wss://stream.binance.com socket
# ---------------------------------------------------------------------------


@router.get("/ticks")
async def get_ticks(binance: str = Query("", description="Comma-separated Binance slugs")) -> Dict[str, Any]:
    """Latest cached tick per symbol, from the backend's Binance socket.

    Requesting a symbol also SUBSCRIBES to it (and refreshes its TTL), so the
    stream follows the operator's watchlist without a separate registration
    call. A symbol asked for the first time returns null until the first frame
    arrives — a second or so — rather than a fabricated price.
    """
    slugs = [s.strip().lower() for s in binance.split(",") if s.strip()]
    stream = get_ticker_stream()
    accepted = stream.register(slugs)
    return {
        "ticks": stream.snapshot(accepted),
        "requested": slugs,
        "stream": stream.status(),
    }


# ---------------------------------------------------------------------------
# Multi-exchange — replaces app/api/multiexchange/route.ts
# ---------------------------------------------------------------------------

# (ExchangeId, ticker URL template, JSON path to the last price)
#
# The ids match `ExchangeId` in lib/multiExchange.ts exactly, because the
# response below reproduces that file's `MultiExchangeSnapshot` shape and
# `EXCHANGE_LABELS` is keyed on them. A new id here without one there renders as
# an unlabelled row.
_VENUES = [
    ("binance", f"{BINANCE_SPOT}/api/v3/ticker/price?symbol={{sym}}", ("price",)),
    ("bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol={sym}",
     ("result", "list", 0, "lastPrice")),
    ("okx", "https://www.okx.com/api/v5/market/ticker?instId={dash}", ("data", 0, "last")),
    ("kraken", "https://api.kraken.com/0/public/Ticker?pair={kraken}", None),
    ("coinbase", "https://api.exchange.coinbase.com/products/{dash}/ticker", ("price",)),
    # `data` is a single-element LIST even when one instrument is requested, and
    # `a` is the latest trade price in Crypto.com's v2 ticker.
    ("cryptocom", "https://api.crypto.com/v2/public/get-ticker?instrument_name={under}",
     ("result", "data", 0, "a")),
]

# Kraken calls Bitcoin XBT. Without this the pair lookup silently returns an
# empty result set, which reads as "Kraken is down" rather than "we asked for a
# symbol Kraken does not use".
_KRAKEN_BASE_OVERRIDES = {"BTC": "XBT"}


def _dig(data: Any, path) -> Any:
    for key in path:
        if data is None:
            return None
        try:
            data = data[key]
        except (KeyError, IndexError, TypeError):
            return None
    return data


@router.get("/multiexchange")
async def get_multi_exchange(
    symbol: str = Query(..., description="Binance-style slug, e.g. BTCUSDT"),
    base: str = Query("", description="Base asset, e.g. BTC"),
    quote: str = Query("USDT", description="Quote asset, e.g. USDT"),
) -> Dict[str, Any]:
    """One symbol priced across several venues, each degrading independently.

    RETURNS lib/multiExchange.ts's `MultiExchangeSnapshot` SHAPE — `quotes` as a
    discriminated union on `ok`, not a venue list with nullable prices. That file
    still owns `computeSpread` and `buildMultiExchangeContext`, both of which
    narrow on `q.ok`, and `MultiExchangePanel` renders from the same type. A
    tidier shape here would mean rewriting all three for no gain.

    A venue that fails is reported with its reason rather than dropped: a missing
    venue reads as "not checked", and only "checked, and here is why there is no
    number" is true. Two venues disagreeing by more than the normal spread is
    usually a stale feed rather than an arbitrage, which is the whole point of
    asking several.
    """
    sym = _clean_symbol(symbol, field="symbol").upper()
    base_asset = (base or sym.replace(quote.upper(), "")).upper()
    quote_asset = quote.upper()
    dashed = f"{base_asset}-{quote_asset}"
    under = f"{base_asset}_{quote_asset}"
    kraken_pair = f"{_KRAKEN_BASE_OVERRIDES.get(base_asset, base_asset)}{quote_asset}"

    async def one(exchange_id: str, template: str, path) -> Dict[str, Any]:
        url = template.format(sym=sym, dash=dashed, under=under, kraken=kraken_pair)
        result = await fetch_json(url, label=f"{exchange_id} ticker", retries=0)

        if not result.ok:
            return {"exchange": exchange_id, "ok": False, "error": result.error or "no response"}

        if path is None:
            # Kraken nests the price under an unpredictable pair key (XXBTZUSD
            # and friends), so the first entry is taken rather than guessing.
            pairs = (result.data or {}).get("result") or {}
            first = next(iter(pairs.values()), None) if isinstance(pairs, dict) else None
            raw = _dig(first, ("c", 0)) if first else None
        else:
            raw = _dig(result.data, path)
            # Crypto.com returns `a` (best ask) inside a single-element list on
            # some responses and as a scalar on others.
            if isinstance(raw, list):
                raw = raw[0] if raw else None

        price = _num(raw)
        if price is None:
            return {
                "exchange": exchange_id,
                "ok": False,
                "error": "venue answered but carried no usable price for this pair",
            }
        return {"exchange": exchange_id, "ok": True, "price": price, "quoteCurrency": quote_asset}

    quotes = await asyncio.gather(*(one(eid, tpl, path) for eid, tpl, path in _VENUES))

    return {
        "symbol": f"{base_asset}/{quote_asset}",
        "quotes": list(quotes),
        "fetchedAt": int(time.time() * 1000),
    }


# ---------------------------------------------------------------------------
# News — replaces app/api/news/route.ts
# ---------------------------------------------------------------------------

# Keyless RSS feeds only. The keyed providers (apitube, gnews, newsdata, newsx)
# stay on the Next.js side for now: their keys live in Vercel's environment and
# moving a secret between two deployments is a separate, deliberate operation
# from fixing a geo-block. Nothing here is geo-restricted, so leaving them is
# not leaving a bug behind — see the note in app/api/news/route.ts.
ATOM_NS = "http://www.w3.org/2005/Atom"

# The same list app/api/news/route.ts used, moved here.
#
# `www.binance.com` is why this is not optional. A Binance host refuses a
# restricted region with the same 451 as the market endpoints, so the news panel
# carried the identical latent failure — it would simply have shown fewer
# headlines rather than an error, which is the harder version to notice.
#
# The keyed aggregators (APITube, GNews, NewsX, NewsData) deliberately stay on
# the Next.js side: their keys and their daily-usage counter live there, none of
# them is geo-restricted, and moving a secret between two deployments is a
# separate and deliberate operation from fixing a geo-block. See the note in
# app/api/news/route.ts.
_RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("WSJ Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("Binance Announcements", "https://www.binance.com/en/support/announcement/rss"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("Coinbase Blog", "https://blog.coinbase.com/feed"),
    ("Kraken Blog", "https://blog.kraken.com/feed/"),
]


def _parse_rss(xml_text: str, source: str, limit: int) -> List[Dict[str, Any]]:
    """Minimal RSS/Atom item extraction using the stdlib.

    `feedparser` would be nicer and is one more dependency for six well-formed
    feeds. `ElementTree` is in the standard library and enough for title, link
    and date, which is all the frontend renders.
    """
    items: List[Dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return items

    # RSS puts items at channel/item; Atom uses a namespaced <entry>.
    nodes = root.findall(".//item") or root.findall(f".//{{{ATOM_NS}}}entry")

    for node in nodes[:limit]:
        def text(tag: str) -> Optional[str]:
            """Read a child element's text, plain tag or Atom-namespaced.

            EVERY LOOKUP IS `is not None`, NEVER `or`, AND THAT IS THE WHOLE BUG
            THIS FUNCTION ALREADY HAD.

            `ElementTree.Element` defines `__len__` as its number of CHILDREN,
            so a leaf element — which is every element we want here, `<title>`,
            `<link>`, `<pubDate>` — is FALSY. Written as
            `node.find(tag) or node.find(ns + tag)`, a found `<title>` was
            discarded as falsy and the namespaced lookup returned None, so every
            article was skipped for having no title. The feeds fetched fine and
            reported `available: true` with zero articles, which reads as "the
            feed is empty" rather than "the parser is broken".
            """
            found = node.find(tag)
            if found is None:
                found = node.find(f"{{{ATOM_NS}}}{tag}")
            if found is None or not found.text:
                return None
            return found.text.strip() or None

        title = text("title")
        if not title:
            continue

        link = text("link")
        if not link:
            # Atom carries the URL in an attribute rather than as text.
            atom_link = node.find(f"{{{ATOM_NS}}}link")
            if atom_link is not None:
                link = atom_link.get("href")

        # Field names are `link` and `pubDate`, NOT `url`/`publishedAt`, because
        # that is the NewsItem shape components/NewsPanel.tsx and
        # components/MarketIntel.tsx already parse. Renaming them here would turn
        # a networking fix into a UI change for no benefit.
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "pubDate": text("pubDate") or text("published") or text("updated"),
        })
    return items


@router.get("/news")
async def get_news(limit: int = Query(40, ge=1, le=100)) -> Dict[str, Any]:
    """Headlines from the keyless RSS feeds.

    Each feed is fetched independently and its outcome recorded separately. One
    dead feed must not empty the whole panel, and the panel must be able to say
    WHICH source is missing — a feed that quietly returns nothing is
    indistinguishable from a slow news day, and this list contains a Binance host
    that a blocked region would silently drop.
    """
    results = await asyncio.gather(
        *(fetch_text(url, label=f"{name} RSS") for name, url in _RSS_FEEDS)
    )

    items: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    per_feed = max(1, limit // len(_RSS_FEEDS) + 1)

    for (name, _), result in zip(_RSS_FEEDS, results):
        if not result.ok:
            sources.append({"source": name, "available": False, "error": result.error, "count": 0})
            continue
        parsed = _parse_rss(result.data or "", name, per_feed)
        sources.append({"source": name, "available": True, "error": None, "count": len(parsed)})
        items.extend(parsed)

    # Newest first. An unparseable date sorts last rather than being dropped —
    # the headline is still real even when its timestamp is not readable.
    def sort_key(item: Dict[str, Any]) -> float:
        raw = item.get("pubDate")
        if not raw:
            return 0.0
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError):
            return 0.0

    items.sort(key=sort_key, reverse=True)

    return {
        # `items`, matching the key the frontend already reads.
        "items": items[:limit],
        "sources": sources,
        "availableCount": sum(1 for s in sources if s["available"]),
        "totalCount": len(sources),
    }


# ---------------------------------------------------------------------------
# Reachability — replaces the Binance probe in app/api/health/route.ts
# ---------------------------------------------------------------------------


@router.get("/upstream-health")
async def upstream_health() -> Dict[str, Any]:
    """Can THIS host actually reach each upstream? The first thing to check.

    This is the endpoint that answers the question the 451 raised: is the
    backend's own region served by these providers? It is cheap, keyless, and
    names the geo-block explicitly when it sees one, so an operator does not
    have to infer it from a 502 three layers up.
    """
    checks = await asyncio.gather(
        fetch_json(f"{BINANCE_SPOT}/api/v3/ping", label="Binance spot", retries=0),
        fetch_json(f"{BINANCE_FUTURES}/fapi/v1/ping", label="Binance futures", retries=0),
        fetch_json(f"{YAHOO}/v8/finance/chart/AAPL?interval=1d&range=1d", label="Yahoo", retries=0),
        fetch_json(f"{FEAR_GREED}/fng/?limit=1", label="Fear & Greed", retries=0),
    )
    labels = ["binanceSpot", "binanceFutures", "yahoo", "fearGreed"]

    upstreams = {}
    for label, result in zip(labels, checks):
        upstreams[label] = {
            "reachable": result.ok,
            "status": result.status,
            "geoBlocked": result.geo_blocked,
            "error": result.error,
        }

    blocked = [k for k, v in upstreams.items() if v["geoBlocked"]]
    return {
        "upstreams": upstreams,
        "allReachable": all(v["reachable"] for v in upstreams.values()),
        "geoBlocked": blocked,
        "note": (
            f"This host's region is REFUSED by: {', '.join(blocked)}. Moving these calls off "
            f"Vercel fixed Vercel's region — this host has the same problem and the data will "
            f"not load until it runs somewhere served."
            if blocked else
            "This host's region is served by every upstream checked."
        ),
        "tickerStream": get_ticker_stream().status(),
    }
