"""freenewsapi.io for NEWS, tinyfish.ai for WEB SEARCH — and the line between.

The operator supplied both keys and was specific: news from one, web search
from the other. They are wired differently because they are different kinds of
thing, and the difference is enforced here rather than written down.

NEWS joins the four RSS feeds as a fifth HEADLINE source, in the same item
shape, so `score_headlines` has one representation of a headline to understand.

WEB SEARCH IS NOT A TRADING INPUT. A web search is a live, unbounded,
non-replayable fetch: a graph node calling it would break Section 39.4, so a
resumed checkpoint would reason over different text than the original decision.
`validate_market_data` is the single fetch point for everything a decision
reads, and this is deliberately not in it.

NEITHER CAN BLOCK A TRADE. Both return None on failure and never raise, so an
outage costs one optional input rather than the trading plane.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest


# ---------------------------------------------------------------------------
# Keys, read at call time and in the spelling `.env` actually uses
# ---------------------------------------------------------------------------

def test_the_lowercase_env_spelling_is_what_is_read(monkeypatch):
    """`.env` carries `freenewsapi` and `tinyfish` in lowercase. Reading only
    the uppercase form would leave both silently unconfigured on the operator's
    own deployment — the integration would be present and inert."""
    from backend.services import news_search as ns

    for name in ("freenewsapi", "FREENEWSAPI", "FREE_NEWS_API_KEY",
                 "tinyfish", "TINYFISH", "TINYFISH_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("freenewsapi", "lower-news")
    monkeypatch.setenv("tinyfish", "lower-search")
    assert ns.freenews_key() == "lower-news"
    assert ns.tinyfish_key() == "lower-search"


def test_the_uppercase_spelling_also_works(monkeypatch):
    """An operator who later uppercases them must not silently lose the
    integration."""
    from backend.services import news_search as ns

    for name in ("freenewsapi", "FREENEWSAPI", "tinyfish", "TINYFISH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FREENEWSAPI", "upper-news")
    monkeypatch.setenv("TINYFISH", "upper-search")
    assert ns.freenews_key() == "upper-news"
    assert ns.tinyfish_key() == "upper-search"


@pytest.mark.asyncio
async def test_no_key_DISABLES_the_source_rather_than_erroring(monkeypatch):
    """A deployment without the keys must behave exactly as it did before this
    module existed."""
    from backend.services import news_search as ns

    for name in ("freenewsapi", "FREENEWSAPI", "FREE_NEWS_API_KEY",
                 "tinyfish", "TINYFISH", "TINYFISH_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert await ns.fetch_freenews("bitcoin") is None
    assert await ns.tinyfish_search("bitcoin") is None


def test_status_never_returns_a_key(monkeypatch):
    from backend.services import news_search as ns

    monkeypatch.setenv("freenewsapi", "SECRET-NEWS-KEY")
    monkeypatch.setenv("tinyfish", "SECRET-SEARCH-KEY")
    blob = repr(ns.status())
    assert "SECRET-NEWS-KEY" not in blob
    assert "SECRET-SEARCH-KEY" not in blob
    assert ns.status()["news"]["configured"] is True


# ---------------------------------------------------------------------------
# WEB SEARCH MAY NOT REACH A DECISION
# ---------------------------------------------------------------------------

def test_no_graph_node_imports_the_web_search():
    """THE CONSTRAINT, ENFORCED RATHER THAN DOCUMENTED.

    A web search is a live, unbounded, non-replayable fetch. A node calling one
    would not be replay-safe (Section 39.4): a resumed checkpoint would reason
    over different text than the original decision made. That is the same rule
    that keeps the order book and the tape fetched at the single fetch point
    and never inside a specialist.
    """
    nodes = pathlib.Path("backend/graphs")
    offenders = []
    for path in nodes.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "tinyfish_search" in text or "TINYFISH_SEARCH_URL" in text:
            offenders.append(str(path))
    assert not offenders, (
        f"web search reached the graph in {offenders} — a live fetch inside a "
        f"decision is not replay-safe"
    )


def test_the_module_says_which_one_is_a_trading_input():
    from backend.services import news_search as ns

    note = ns.status()["webSearch"]["role"]
    assert "No graph node reads it" in note
    assert "replay-safe" in note


# ---------------------------------------------------------------------------
# NEWS joins the feed, and cannot break it
# ---------------------------------------------------------------------------

def test_freenews_is_a_FIFTH_source_not_a_replacement():
    """More sources is the right shape: the news specialist weights by
    COVERAGE, and one provider's editorial mix carrying the whole signal is
    worse than four feeds plus one API."""
    import backend.services.microstructure_feed as mf

    src = inspect.getsource(mf.fetch_headlines)
    assert "fetch_freenews" in src
    assert len(mf.RSS_FEEDS) >= 4, "the RSS feeds were removed rather than joined"


def test_a_broken_news_api_is_NAMED_not_merely_counted():
    """"3 of 5 answered" does not tell an operator which one went — the rule
    the RSS failures already follow."""
    import backend.services.microstructure_feed as mf

    src = inspect.getsource(mf.fetch_headlines)
    assert "FreeNewsAPI" in src
    assert "failed.append" in src


def test_a_broken_news_api_cannot_raise_out_of_the_feed():
    """An optional source that raised would turn an input into an outage."""
    import backend.services.microstructure_feed as mf

    src = inspect.getsource(mf.fetch_headlines)
    # The guard sits AFTER the call (a try/except wrapping it), so the window
    # has to look forward far enough to contain the handler rather than
    # backward from the call site.
    block = src[src.index("fetch_freenews"):]
    assert "except Exception" in block[:900]


@pytest.mark.asyncio
async def test_a_non_200_is_None_and_never_an_empty_list(monkeypatch):
    """None is "could not ask"; [] is "asked, nothing matched". The news
    specialist has to tell those apart — reporting a dead feed as "no news" is
    a defect this project has already paid for."""
    from backend.services import news_search as ns

    from backend.services import upstream

    async def _refused(url, **kwargs):
        return upstream.UpstreamResult(
            data=None, error="FreeNewsAPI: HTTP 401", status=401
        )

    monkeypatch.setenv("freenewsapi", "x")
    monkeypatch.setattr(upstream, "fetch_json", _refused)
    assert await ns.fetch_freenews("bitcoin") is None


@pytest.mark.asyncio
async def test_a_parsed_headline_matches_the_RSS_item_shape(monkeypatch):
    """One representation of a headline, so a field like `pubDate` cannot
    quietly stop being read for one source."""
    from backend.services import news_search as ns

    payload = {
        "data": [
            {
                "uuid": "abc",
                "title": "Bitcoin moves",
                "published_at": "2026-10-29T00:00:00Z",
                "publisher": {"name": "Example Wire"},
                "original_url": "https://example.com/a",
            }
        ],
        "meta": {"page_size": 10},
    }

    from backend.services import upstream

    async def _answered(url, **kwargs):
        # The KEY MUST BE IN THE HEADER, not the query string — a provider that
        # reads only the header would otherwise 401 in production while this
        # test passed.
        assert kwargs.get("headers", {}).get("x-api-key") == "x"
        return upstream.UpstreamResult(data=payload, status=200)

    monkeypatch.setenv("freenewsapi", "x")
    monkeypatch.setattr(upstream, "fetch_json", _answered)

    items = await ns.fetch_freenews("bitcoin")
    assert items and len(items) == 1
    assert set(items[0]) == {"title", "link", "source", "pubDate"}
    assert items[0]["title"] == "Bitcoin moves"
    assert items[0]["source"] == "Example Wire"
    assert items[0]["pubDate"] == "2026-10-29T00:00:00Z"


def test_both_go_through_the_ONE_outbound_helper():
    """A fresh `httpx.AsyncClient` here was a REGRESSION that hung the suite at
    78%: `validate_market_data` calls `fetch_headlines`, so this ran inside
    every graph test, and the network guard patches `socket.connect` — which a
    DNS lookup happens before. `upstream.fetch_json` is the shared client with
    the bounded timeout and the 4xx-not-retried policy, and a second ad-hoc
    client is how one timeout policy becomes two."""
    import inspect as _i

    from tests.sourceutil import code_only

    from backend.services import news_search as ns

    raw = _i.getsource(ns)
    assert "upstream import fetch_json" in raw

    # STRIPPED, because the comment above the fix deliberately NAMES the thing
    # it warns about — the sixth time a source-scanning test here has first
    # failed on the explanation rather than the code. A mention is not a call,
    # and the answer is always `code_only`, never a weaker comment.
    assert "httpx" not in code_only(raw), "a second outbound client came back"


def test_the_live_verification_is_a_SCRIPT_not_a_test():
    """`conftest` blocks the network for every test on purpose, and verifying
    these needs real authenticated calls with the operator's own keys — the
    same arrangement as the Bybit and Binance demo round trips."""
    path = pathlib.Path("scripts/verify_news_search.py")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    # It must print the RAW shape, so a renamed field is diagnosable rather
    # than invisible.
    assert "top-level keys" in text
