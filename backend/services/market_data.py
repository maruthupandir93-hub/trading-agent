import logging
import asyncio
from backend.core.config import settings
from backend.services.live_market_data import get_live_price
from backend.services.exchange_client import get_exchange_client

logger = logging.getLogger(__name__)

_prices = {}

async def fetch_prices():
    """Refresh the polled price cache from USDT perpetuals.

    THE SYMBOL FILTER USED TO LIVE HERE AND WAS WRONG. It tested
    `symbol.endswith("/USDT")` against ccxt keys that, under
    `defaultType='future'`, look like `BTC/USDT:USDT`. It matched nothing, so the
    cache was permanently empty and this function's own error line blamed the
    network. The filter now lives in
    `ExchangeClient.fetch_usdt_perpetual_prices()`, where it can use typed market
    metadata instead of guessing at ccxt's symbol formatting — that docstring has
    the full account.

    RETRIES ONLY WHAT RETRYING CAN FIX. An empty result is NOT retried: if the
    exchange answered and nothing matched, asking again returns the same
    non-match, and burning the retry budget on it produced a
    "maximum retries" line that read like an outage. A raised exception is
    retried with backoff, because that is the failure a retry is for.
    """
    global _prices
    client = get_exchange_client()
    max_retries = settings.MAX_RETRIES

    for attempt in range(max_retries):
        try:
            new_prices = await client.fetch_usdt_perpetual_prices()
        except Exception as e:
            wait_time = 2 ** attempt
            logger.warning(
                "Error fetching prices (attempt %d/%d): %s. Retrying in %ds...",
                attempt + 1,
                max_retries,
                e,
                wait_time,
            )
            await asyncio.sleep(wait_time)
            continue

        # None means THE CALL FAILED, which is the case a retry is for. It used
        # to be indistinguishable from an empty match, so this branch never ran
        # and the loop below reported a filter mismatch for a network fault.
        if new_prices is None:
            wait_time = 2 ** attempt
            logger.warning(
                "Price fetch failed (attempt %d/%d) — the client reported a failed "
                "call, not an empty result. Retrying in %ds...",
                attempt + 1,
                max_retries,
                wait_time,
            )
            await asyncio.sleep(wait_time)
            continue

        if new_prices:
            # Replace wholesale rather than merging. A merge would keep a stale
            # price for a symbol the exchange has stopped quoting, and a stale
            # price that looks live is worse than a missing one.
            _prices = new_prices
            logger.debug("Fetched %d prices via CCXT", len(_prices))
            return

        # The client already logged which filter emptied the result, with counts.
        # Do not retry, and do not claim a network failure.
        logger.error(
            "Price refresh produced no usable symbols. The previous cache of %d "
            "price(s) is left in place and is now STALE.",
            len(_prices),
        )
        return

    logger.error(
        "Failed to fetch prices via CCXT after %d attempts — every attempt failed. "
        "The previous cache of %d price(s) is left in place and is now STALE. "
        "This IS a call failure (network, rate limit or a non-JSON body from the "
        "exchange), not the symbol-filter mismatch that used to share this message.",
        max_retries,
        len(_prices),
    )

def get_price(symbol: str) -> float:
    """Last known price for `symbol`, or 0.0 when none of the caches has one.

    THREE SOURCES, IN FRESHNESS ORDER, AND THE THIRD WAS MISSING
    ------------------------------------------------------------
    This system keeps three independent price caches, and this function is what
    every graph node and the operator trade panel read:

      1. `live_market_data._live_prices`  the agent's own websocket feed
      2. `ticker_stream`                  the dashboard's Binance combined stream
      3. `_prices`                        the polled ccxt futures cache

    Source 2 was NOT consulted, and the gap was visible rather than theoretical.
    For the first stretch after startup 1 and 3 are both empty while 2 is already
    connected and ticking, because the dashboard subscribed it. A graph run in
    that window aborted at `data_validation` with

        no live price for BTC/USDT (feed returned 0.0)

    having produced nothing, while `/api/marketdata/ticks` was serving a live
    price for that exact symbol at that exact moment. The operator trade panel
    showed the same hole as a null price.

    THIS IS NOT A FALLBACK TO SOMETHING WEAKER. Source 2 is the same Binance
    ticker feed as source 1, over a socket this process already holds, and
    `price_for` refuses a tick older than the stream's own staleness threshold
    rather than handing back a price from minutes ago. It is ordered third only
    because the first two are the agent's own feeds.

    Returns 0.0 when nothing has a price — callers test `price <= 0` and report
    the symbol unmeasurable rather than computing against it.
    """
    # 1. The agent's own websocket feed.
    live_price = get_live_price(symbol)
    if live_price > 0:
        return live_price

    # 2. The dashboard's ticker stream. Fresh, and usually populated first.
    try:
        from backend.services.ticker_stream import get_ticker_stream

        streamed = get_ticker_stream().price_for(symbol)
        if streamed and streamed > 0:
            return streamed
    except Exception:  # noqa: BLE001
        # Never let a price lookup raise into a node. A missing price is handled
        # everywhere; an exception here is not.
        pass

    # 3. The polled ccxt cache.
    return _prices.get(symbol, 0.0)

async def fetch_klines(symbol: str, interval: str, limit: int = 100) -> list:
    """
    Fetch historical klines via CCXT with exponential backoff.
    """
    client = get_exchange_client()
    max_retries = settings.MAX_RETRIES
    
    for attempt in range(max_retries):
        try:
            # CCXT fetch_ohlcv returns: [ [timestamp, open, high, low, close, volume], ... ]
            ohlcv_data = await client.fetch_ohlcv(symbol, interval, limit=limit)
            
            klines = []
            for k in ohlcv_data:
                klines.append({
                    "openTime": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "closeTime": k[0] + 60000 # Approximation, CCXT does not return closeTime directly
                })
            return klines
        except Exception as e:
            wait_time = 2 ** attempt
            logger.warning(f"Error fetching klines for {symbol} (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
            
    logger.error(f"Failed to fetch klines for {symbol} after maximum retries.")
    return []
