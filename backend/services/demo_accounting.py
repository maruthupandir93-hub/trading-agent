"""Read-only reconciliation of a fully closed Binance demo round trip.

Called after the close fills, never between an entry fill and its protection.
Missing, partial, foreign-currency or truncated evidence returns None. The
caller then keeps costs explicitly modelled instead of inventing measurements.
"""
import asyncio
import datetime as dt
import logging
from decimal import Decimal

import httpx

from backend.core.db import get_db_pool
from backend.services import binance_testnet as demo

logger = logging.getLogger(__name__)


def measured_leg(rows, order_id, quantity):
    if not isinstance(rows, list) or len(rows) >= 1000:
        return None
    fills = {str(r["id"]): r for r in rows
             if str(r.get("orderId")) == str(order_id)}
    if not fills or any(r.get("commissionAsset") != "USDT" for r in fills.values()):
        return None
    qty = sum(Decimal(str(r["qty"])) for r in fills.values())
    if abs(qty - Decimal(str(quantity))) > Decimal("0.00000001"):
        return None
    fee = sum(Decimal(str(r["commission"])) for r in fills.values())
    gross = sum(Decimal(str(r["realizedPnl"])) for r in fills.values())
    if not fee.is_finite() or not gross.is_finite() or fee < 0:
        return None
    return fee, gross


async def _read(tar_id, symbol, exit_order_id, quantity, opened_at, closed_at):
    pool = get_db_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT order_id FROM execution_quality WHERE tar_id=$1 "
            "AND exchange='binance_testnet' ORDER BY ts DESC LIMIT 1", str(tar_id))
    if not row:
        return None
    entry_id = row["order_id"]
    def ms(value):
        return int(value.replace(tzinfo=dt.timezone.utc).timestamp() * 1000) if value.tzinfo is None else int(value.timestamp()*1000)
    start, end = ms(opened_at), ms(closed_at)
    if end - start >= 7 * 86400000:
        return None  # This bounded query cannot certify a paginated long hold.
    sym = demo.to_binance_symbol(symbol)
    async with httpx.AsyncClient(timeout=3) as client:
        results = await asyncio.gather(
            demo._signed(client, "GET", "/fapi/v1/userTrades", {"symbol": sym, "orderId": entry_id, "limit": 1000}),
            demo._signed(client, "GET", "/fapi/v1/userTrades", {"symbol": sym, "orderId": exit_order_id, "limit": 1000}),
            demo._signed(client, "GET", "/fapi/v1/income", {"symbol": sym, "incomeType": "FUNDING_FEE", "startTime": start, "endTime": end, "limit": 1000}),
        )
    if not all(ok for ok, _ in results):
        return None
    entry = measured_leg(results[0][1], entry_id, quantity)
    exit = measured_leg(results[1][1], exit_order_id, quantity)
    income = results[2][1]
    if entry is None or exit is None or not isinstance(income, list) or len(income) >= 1000:
        return None
    if any(r.get("asset") != "USDT" or r.get("symbol") != sym for r in income):
        return None
    funding = -sum((Decimal(str(r["income"])) for r in income), Decimal(0))
    if not funding.is_finite():
        return None
    return dict(entry_order_id=str(entry_id), entry_fee=float(entry[0]),
                exit_fee=float(exit[0]), funding=float(funding),
                realized=float(entry[1] + exit[1] - entry[0] - exit[0] - funding))


async def closed_round_trip(**kwargs):
    try:
        return await asyncio.wait_for(_read(**kwargs), timeout=4)
    except Exception as exc:
        logger.warning("Demo accounting remains estimated (%s)", type(exc).__name__)
        return None
