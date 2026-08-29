"""Headline scoring for the Section 9 news specialist.

WHAT THIS UNBLOCKS
------------------
`graphs/nodes/specialists.py::specialist_news` was hardcoded unavailable with:

    "no news, filing or social feed is ingested anywhere in this backend: there
     is no headline source to score, so event risk around this symbol is unknown
     rather than absent"

That was true of the reasoning layer and false of the system. `api/marketdata.py`
already ingests four keyless RSS feeds (CoinDesk, Cointelegraph, CryptoSlate,
Binance announcements) and returns tagged, timestamped headlines — verified live.
The feed existed for the dashboard's news panel and was never handed to the panel
that votes.

It cost 1.5 of 7.0 directional weight, and because `run_debate` scales confidence
by coverage, that missing weight was applied as a multiplier to every verdict the
agent ever produced.

WHY THIS IS KEYWORD SCORING AND NOT A MODEL CALL
------------------------------------------------
CLAUDE.md: *"Deterministic over LLM where the math is real ... asking a model to
'reason over' numbers already on hand adds hallucination risk to a financial
decision for no benefit and isn't reproducible."*

Headlines are not numbers, so that argument is weaker here than it is for the
debate moderator — but two others take its place. This runs on every graph run
for every symbol, and the tiered-model cost control exists precisely to keep that
kind of call off the hot path. And a model asked "is this bullish?" will answer
even when the headline is about an unrelated asset, which is the failure mode
that matters most: a confident directional vote sourced from noise.

So this is a transparent lexicon. Every point of the score traces to a term a
reader can see in `_BULLISH` / `_BEARISH` below, and two runs over the same
headlines produce the same number.

WHAT IT DELIBERATELY DOES NOT CLAIM
-----------------------------------
  * It is not sentiment analysis. It counts occurrences of terms with a known
    directional lean in this domain. "Bitcoin ETF outflows accelerate" scores
    bearish because "outflows" does, not because anything read the sentence.
  * Negation and sarcasm are not handled, and pretending otherwise would be
    worse than stating it. This is why the confidence ceiling below exists.
  * `RELEVANCE` filtering is by symbol keyword. A headline that never names the
    asset or its ecosystem does not vote, because a general market headline
    scored as evidence about SOL is evidence about nothing.

The confidence this produces is CAPPED well below 1.0 for those reasons. A
lexicon over four RSS feeds is real evidence and weak evidence, and the cap is
where that judgement is written down rather than implied.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# A lexicon cannot support a strong vote. 0.45 is the most this specialist may
# ever contribute, so it can shade a verdict and can never carry one.
MAX_CONFIDENCE = 0.45

# Headlines older than this are dropped. A three-day-old story is not evidence
# about the next fifteen minutes, and letting stale items accumulate would make
# the reading drift toward whatever the week's theme was.
MAX_AGE_SECONDS = 24 * 3600

# Below this many relevant headlines nothing is claimed. Two headlines agreeing
# is a coincidence; it is not a measurement.
MIN_RELEVANT = 3

# Neutral band on the net score. Inside it the reading is 'neutral' — measured
# and found balanced, which is a different finding from unavailable.
NEUTRAL_BAND = 0.20


# Terms carrying a directional lean in crypto-market coverage. Weighted: a
# regulatory approval moves a market more than the word "gains" does.
_BULLISH: Dict[str, float] = {
    "surge": 1.0, "surges": 1.0, "soar": 1.0, "soars": 1.0, "rally": 1.0,
    "rallies": 1.0, "jump": 0.7, "jumps": 0.7, "gains": 0.6, "climbs": 0.6,
    "rises": 0.6, "record high": 1.5, "all-time high": 1.5, "breakout": 1.0,
    "approval": 1.5, "approved": 1.5, "adoption": 1.0, "inflow": 1.2,
    "inflows": 1.2, "accumulate": 0.8, "accumulation": 0.8, "bullish": 1.2,
    "upgrade": 0.8, "partnership": 0.6, "integration": 0.5, "launch": 0.5,
    "buyback": 0.9, "halving": 0.7, "institutional": 0.5, "treasury": 0.6,
}

_BEARISH: Dict[str, float] = {
    "plunge": 1.0, "plunges": 1.0, "crash": 1.5, "crashes": 1.5, "slump": 1.0,
    "slumps": 1.0, "tumble": 1.0, "tumbles": 1.0, "falls": 0.6, "drops": 0.6,
    "sinks": 0.8, "decline": 0.6, "declines": 0.6, "selloff": 1.2,
    "sell-off": 1.2, "liquidation": 1.2, "liquidations": 1.2, "outflow": 1.2,
    "outflows": 1.2, "hack": 1.5, "hacked": 1.5, "exploit": 1.5,
    "breach": 1.2, "lawsuit": 1.0, "sued": 1.0, "ban": 1.3, "banned": 1.3,
    "crackdown": 1.3, "investigation": 0.9, "probe": 0.9, "bearish": 1.2,
    "downgrade": 0.8, "bankruptcy": 1.5, "insolvency": 1.5, "fraud": 1.4,
    "delisting": 1.2, "delisted": 1.2, "unlock": 0.8, "dump": 1.0,
}

# EVENT-RISK terms. These do not vote on direction — they raise the chance that a
# technical thesis is about to be invalidated by something no chart can see. The
# news blocker's own text called this out: "a scheduled unlock or listing would
# invalidate a technical thesis without any other specialist noticing."
_EVENT_RISK: Dict[str, float] = {
    "sec": 0.6, "regulator": 0.6, "regulatory": 0.6, "fed": 0.7,
    "fomc": 0.9, "rate decision": 0.9, "cpi": 0.8, "inflation": 0.5,
    "unlock": 0.8, "listing": 0.5, "hard fork": 0.7, "upgrade": 0.4,
    "hack": 1.0, "exploit": 1.0, "lawsuit": 0.7, "investigation": 0.6,
    "halt": 0.8, "outage": 0.7, "jackson hole": 0.8,
}

# Which words make a headline about a given asset. Base symbol -> the terms that
# count as naming it. Anything not listed falls back to the base ticker alone.
_SYMBOL_TERMS: Dict[str, Sequence[str]] = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ether", "ethereum"),
    "SOL": ("sol", "solana"),
    "BNB": ("bnb", "binance coin"),
    "XRP": ("xrp", "ripple"),
    "ADA": ("ada", "cardano"),
    "DOGE": ("doge", "dogecoin"),
    "AVAX": ("avax", "avalanche"),
    "LINK": ("link", "chainlink"),
    "MATIC": ("matic", "polygon"),
    "DOT": ("dot", "polkadot"),
}

# Terms that make a headline market-wide rather than asset-specific. These count
# for ANY symbol at reduced weight — a Fed decision moves everything, and
# discarding it because it does not say "bitcoin" would drop the most
# market-moving headlines on the feed.
_MARKET_WIDE = (
    "crypto", "cryptocurrency", "digital asset", "altcoin", "stablecoin",
    "federal reserve", "fed ", "fomc", "cpi", "inflation", "jackson hole",
    "etf", "sec ", "regulation", "regulatory",
)
MARKET_WIDE_WEIGHT = 0.5


@dataclass
class NewsReading:
    """What the headline feed says about one symbol, right now."""

    available: bool
    reason: Optional[str] = None
    # 'supports_long' | 'supports_short' | 'neutral', or None when unavailable.
    stance: Optional[str] = None
    confidence: Optional[float] = None
    # 0.0-1.0. How much scheduled/announced risk is in the window. Reported
    # separately from direction because event risk is not directional: an SEC
    # decision is a reason to be less certain, not a reason to be short.
    event_risk: Optional[float] = None
    headlines_scanned: int = 0
    headlines_relevant: int = 0
    top_headlines: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)


def base_symbol(symbol: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC'. Handles every shape this codebase uses.

    Three symbol spellings coexist here — 'BTC/USDT' (ccxt spot), 'BTC/USDT:USDT'
    (ccxt futures) and 'BTCUSDT' (Binance slug) — and `market_data.fetch_prices`
    already lost a month to matching the wrong one. So this strips both the
    settlement suffix and the quote currency rather than assuming a format.
    """
    s = (symbol or "").upper().strip()
    if not s:
        return ""
    s = s.split(":", 1)[0]
    if "/" in s:
        return s.split("/", 1)[0]
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _terms_for(symbol: str) -> Sequence[str]:
    base = base_symbol(symbol)
    return _SYMBOL_TERMS.get(base, (base.lower(),) if base else ())


def _age_seconds(pub_date: Optional[str], now: float) -> Optional[float]:
    """Seconds since publication, or None when the timestamp is unparseable.

    None is NOT treated as fresh. An item with no usable date is dropped by the
    caller, because including it would let an undated feed backfill the window
    with items of unknown age.
    """
    if not pub_date:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(pub_date)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return max(0.0, now - parsed.timestamp())
    except (OSError, OverflowError, ValueError):
        return None


def _score_text(text: str, lexicon: Dict[str, float]) -> float:
    total = 0.0
    for term, weight in lexicon.items():
        # Word-boundary matched so "ban" does not fire on "banking" and "sol"
        # does not fire on "solution" — both of which a naive substring search
        # produces on this feed within minutes.
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            total += weight
    return total


def score_headlines(
    headlines: Optional[Sequence[Dict[str, Any]]],
    symbol: str,
    *,
    now: Optional[float] = None,
) -> NewsReading:
    """Directional lean and event risk for `symbol` from a headline feed.

    `now` is injectable so the function stays pure and testable. Section 39.4: a
    node reading the clock in its own body gets a different answer on replay.
    """
    clock = time.time() if now is None else now
    items = [h for h in (headlines or []) if isinstance(h, dict)]

    if not items:
        return NewsReading(
            available=False,
            reason=(
                "the headline feed returned nothing. That is a FEED failure, not a "
                "quiet news day — unmeasured event risk is not the same as no event risk"
            ),
        )

    bull = 0.0
    bear = 0.0
    event = 0.0
    relevant = 0
    top: List[str] = []
    terms = _terms_for(symbol)

    for item in items:
        title = str(item.get("title") or "").strip()
        if not title:
            continue

        age = _age_seconds(item.get("pubDate"), clock)
        if age is None or age > MAX_AGE_SECONDS:
            continue

        text = title.lower()

        if any(t and t in text for t in terms):
            weight = 1.0
        elif any(t in text for t in _MARKET_WIDE):
            weight = MARKET_WIDE_WEIGHT
        else:
            continue

        relevant += 1
        b = _score_text(text, _BULLISH) * weight
        s = _score_text(text, _BEARISH) * weight
        e = _score_text(text, _EVENT_RISK) * weight
        bull += b
        bear += s
        event += e

        if (b or s or e) and len(top) < 5:
            top.append(f"{title} [{'+' if b >= s else '-'}{max(b, s):.1f}]")

    if relevant < MIN_RELEVANT:
        return NewsReading(
            available=False,
            reason=(
                f"only {relevant} headline(s) in the last {MAX_AGE_SECONDS // 3600}h "
                f"mention {base_symbol(symbol)} or the wider market, below the "
                f"{MIN_RELEVANT} needed. Too few to distinguish a signal from a coincidence"
            ),
            headlines_scanned=len(items),
            headlines_relevant=relevant,
        )

    total = bull + bear
    if total <= 0:
        # Genuinely measured and genuinely flat: relevant headlines exist and none
        # of them carry a directional term. That is a real neutral, not a refusal.
        net = 0.0
    else:
        net = (bull - bear) / total

    if net > NEUTRAL_BAND:
        stance = "supports_long"
    elif net < -NEUTRAL_BAND:
        stance = "supports_short"
    else:
        stance = "neutral"

    # Scaled by how much evidence there is, then capped. Ten agreeing headlines
    # deserve more than three, but no amount of keyword agreement earns a strong
    # directional vote from a lexicon.
    volume_factor = min(1.0, relevant / 10.0)
    confidence = min(MAX_CONFIDENCE, abs(net) * volume_factor)

    # Event risk normalised against a saturation point rather than a maximum, so
    # one very newsy window does not permanently rescale the measure.
    event_risk = min(1.0, event / 5.0)

    evidence = [
        (
            f"{relevant} of {len(items)} headline(s) in the last "
            f"{MAX_AGE_SECONDS // 3600}h mention {base_symbol(symbol)} or the wider market"
        ),
        f"lexicon score: bullish {bull:.1f} vs bearish {bear:.1f} -> net {net:+.2f}",
        (
            f"confidence {confidence:.2f} = |net| x volume {volume_factor:.2f}, "
            f"capped at {MAX_CONFIDENCE} — this is keyword matching, not comprehension"
        ),
        f"event risk {event_risk:.2f} from scheduled/announced-risk terms",
    ]
    evidence.extend(f"headline: {t}" for t in top)

    return NewsReading(
        available=True,
        stance=stance,
        confidence=confidence,
        event_risk=event_risk,
        headlines_scanned=len(items),
        headlines_relevant=relevant,
        top_headlines=top,
        evidence=evidence,
    )
