"""Verify freenewsapi.io and tinyfish.ai against the LIVE services.

WHY THIS IS A SCRIPT AND NOT A TEST. `tests/conftest.py` blocks the network for
every test on purpose, and this makes real authenticated calls with the
operator's own keys. It is the same arrangement as
`scripts/bybit_testnet_roundtrip.py` and `scripts/binance_demo_verify.py`.

WHY IT EXISTS AT ALL. The request shapes in `services/news_search` were taken
from each vendor's published documentation and were NOT exercised against the
wire from the build environment — sending the operator's keys from there was
refused, correctly, as data exfiltration. Until this has been run on the
operator's own machine the adapter is unverified against reality, and this
project's rule is to verify rather than inherit a claim: the same note once
wrongly said `api.binance.com` was unreachable and hid a real bug for months.

It prints the RAW top-level shape of each response before the parsed result,
so a vendor who renames a field produces a readable diagnosis rather than an
empty list.

    .venv/Scripts/python.exe scripts/verify_news_search.py

It reads keys, makes two GET requests and writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# No database, and no chance of touching one.
os.environ.setdefault("DATABASE_URL", "postgresql://verify:verify@192.0.2.1:5432/none")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ["DATABASE_URL"] = "postgresql://verify:verify@192.0.2.1:5432/none"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}ok{RESET}  {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


async def main() -> int:
    import httpx

    from backend.services import news_search as ns

    failures = 0

    # ---- freenewsapi.io ---------------------------------------------------
    print(f"\n{DIM}freenewsapi.io — the fifth headline source{RESET}")
    key = ns.freenews_key()
    if not key:
        bad("no key. `.env` should carry `freenewsapi = <key>`.")
        failures += 1
    else:
        ok(f"key present ({len(key)} chars, never printed)")
        # RAW first, so a renamed field is diagnosable rather than invisible.
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                raw = await client.get(
                    ns.FREENEWS_URL,
                    params={"language": "en", "q": "bitcoin"},
                    headers={"x-api-key": key},
                )
            print(f"      {DIM}HTTP {raw.status_code} from {ns.FREENEWS_URL}{RESET}")
            if raw.status_code == 200:
                body = raw.json()
                keys = sorted(body)[:8] if isinstance(body, dict) else type(body).__name__
                print(f"      {DIM}top-level keys: {keys}{RESET}")
                rows = body.get("data") if isinstance(body, dict) else None
                if isinstance(rows, list) and rows:
                    print(f"      {DIM}first row keys: {sorted(rows[0])[:10]}{RESET}")
            else:
                print(f"      {DIM}{(raw.text or '')[:200]}{RESET}")
        except Exception as exc:  # noqa: BLE001
            print(f"      {DIM}raw probe failed: {type(exc).__name__}: {exc}{RESET}")

        items = await ns.fetch_freenews("bitcoin", limit=5)
        if items is None:
            bad("the adapter could not read it — see the raw probe above")
            failures += 1
        elif not items:
            # NOT a failure. "Asked and nothing matched" is a real answer and
            # the whole point of returning [] rather than None.
            ok("answered, no matching headlines (that is a result, not a fault)")
        else:
            ok(f"{len(items)} headline(s) parsed into the feed's own shape")
            first = items[0]
            print(f"      {DIM}{first.get('source')}: "
                  f"{str(first.get('title'))[:90]}{RESET}")
            for field in ("title", "link", "source", "pubDate"):
                if field not in first:
                    bad(f"the parsed item is missing `{field}` — `score_headlines` reads it")
                    failures += 1

    # ---- tinyfish.ai ------------------------------------------------------
    print(f"\n{DIM}tinyfish.ai — operator web search, NOT a trading input{RESET}")
    key = ns.tinyfish_key()
    if not key:
        bad("no key. `.env` should carry `tinyfish = <key>`.")
        failures += 1
    else:
        ok(f"key present ({len(key)} chars, never printed)")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                raw = await client.get(
                    ns.TINYFISH_SEARCH_URL,
                    params={"query": "bitcoin price today"},
                    headers={"X-API-Key": key},
                )
            print(f"      {DIM}HTTP {raw.status_code} from {ns.TINYFISH_SEARCH_URL}{RESET}")
            if raw.status_code == 200:
                body = raw.json()
                keys = sorted(body)[:8] if isinstance(body, dict) else type(body).__name__
                print(f"      {DIM}top-level keys: {keys}{RESET}")
            else:
                print(f"      {DIM}{(raw.text or '')[:200]}{RESET}")
        except Exception as exc:  # noqa: BLE001
            print(f"      {DIM}raw probe failed: {type(exc).__name__}: {exc}{RESET}")

        results = await ns.tinyfish_search("bitcoin price today", limit=3)
        if results is None:
            bad("the adapter could not read it — see the raw probe above")
            failures += 1
        elif not results:
            ok("answered, no results")
        else:
            ok(f"{len(results)} result(s)")
            print(f"      {DIM}{str(results[0].get('title'))[:90]}{RESET}")

    print()
    if failures:
        print(f"  {RED}{failures} check(s) failed.{RESET} The adapter returns None on "
              f"every failure and never raises, so a broken source DISABLES itself "
              f"rather than stopping a trade.\n")
    else:
        print(f"  {GREEN}both sources verified against the live services{RESET}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
