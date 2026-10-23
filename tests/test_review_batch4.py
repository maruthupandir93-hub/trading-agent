"""Batch 4: six quieter defects, four of them invisible by construction.

The theme in this batch is a value that is WRONG rather than missing, so
nothing errors and nothing logs. A win rate in the wrong units, a direction
that always reads LONG, a password compared as mojibake, a filter that matches
nothing — each one produces a plausible answer, which is why they survive.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# #32 — one field, two units
# ---------------------------------------------------------------------------

def test_every_writer_of_win_rate_uses_the_same_units():
    """`record_trade` and `record_closed_trade` write `(wins/total) * 100`;
    the reset path wrote the bare fraction. So after a reset a 60% win rate
    became 0.6, and nothing validates the field.

    `supervisor_agent._measured_win_rate` feeds KELLY SIZING, so the
    hundred-fold error sized as though the strategy almost never won, and the
    Telegram close alert reported "0.6%" on a winning account.
    """
    import backend.services.ai_memory as mem

    src = inspect.getsource(mem)
    assignments = re.findall(r'\["win_rate"\]\s*=\s*\(?\s*\n?\s*([^\n]+)', src)
    computed = [a for a in assignments if "wins" in a or "/" in a]

    assert computed, "no win_rate computation found — has the field moved?"
    for expr in computed:
        assert "100" in expr, (
            f"a win_rate is computed as a FRACTION here: {expr.strip()!r}. Every "
            f"other writer uses a percentage, and nothing validates the field."
        )


def test_the_recomputed_total_is_a_percentage():
    """Driven, not read: the units are what a caller actually receives."""
    import backend.services.ai_memory as mem

    src = inspect.getsource(mem)
    block = src[src.index('totals["win_rate"]'):]
    block = block[: block.index("memory[")]
    assert "* 100" in block


# ---------------------------------------------------------------------------
# #35 — a correlation cap that read every short as a long
# ---------------------------------------------------------------------------

def test_the_calibrated_event_declares_a_direction():
    """DECLARED, not just passed. Pydantic v2 silently ignores an undeclared
    keyword, which is how `TarApprovedEvent` lost three fields while every
    reader's defensive `getattr` made the chain look like it worked."""
    from backend.models.events import ConfidenceCalibratedEvent

    assert "direction" in ConfidenceCalibratedEvent.model_fields

    event = ConfidenceCalibratedEvent(
        symbol="SOL/USDT", calibrated_confidence=0.4, breakdown={}, direction="SHORT",
    )
    assert event.direction == "SHORT", "the field is declared but does not round-trip"


def test_a_neutral_debate_carries_no_direction():
    """None rather than LONG: a neutral debate has no side, and inventing one
    is the bug this replaces."""
    from backend.models.events import ConfidenceCalibratedEvent

    event = ConfidenceCalibratedEvent(
        symbol="SOL/USDT", calibrated_confidence=0.0, breakdown={},
    )
    assert event.direction is None


def test_the_confidence_agent_carries_the_debates_direction():
    import backend.agents.confidence_agent as ca

    src = code_only(inspect.getsource(ca))
    assert "direction" in src and "winning_direction" in src, (
        "the calibrated event is published without the direction it calibrated"
    )


# ---------------------------------------------------------------------------
# #36 — a tighten dropped mid-flight left the VENUE stop wider
# ---------------------------------------------------------------------------

def test_a_tighten_during_a_replacement_is_re_run_not_dropped():
    """Coalescing alone lost the tightest stop.

    The in-flight replacement re-reads `pos.stop_loss` when it takes the lock,
    which covers a tighten arriving BEFORE that moment. One arriving AFTER it,
    while the cancel/place pair is at the venue, simply returned — and the
    exchange kept the OLDER, WIDER stop.

    In-process that is invisible, because `_check_price` enforces the tighter
    level every tick. It matters exactly when the resting stop matters: if this
    process dies, the venue executes the stale level.
    """
    import backend.agents.position_monitor as pm

    src = code_only(inspect.getsource(pm.PositionMonitorAgent._replace_resting_stop_soon))
    assert "_stop_dirty" in src, "a mid-flight tighten is still dropped"


def test_the_retry_is_bounded():
    """An unbounded loop would spin a cancel and a place per tick against the
    key's rate budget, which is what the coalescing exists to prevent."""
    import backend.agents.position_monitor as pm

    assert isinstance(pm._MAX_STOP_REPLACE_PASSES, int)
    assert 1 <= pm._MAX_STOP_REPLACE_PASSES <= 10


# ---------------------------------------------------------------------------
# #33 — a graph filter that could not reach its own traces
# ---------------------------------------------------------------------------

def test_the_trace_reader_filters_by_filename():
    """`position_monitoring` runs once per tick per open position while
    `trade_analysis` runs once per decision interval, so the newest 200 traces
    were almost always ALL monitoring — and a `trade_analysis` request came
    back EMPTY while 250 of its traces sat on disk."""
    import backend.graphs.tracing as tracing

    assert "graph" in inspect.signature(tracing.list_recent_runs).parameters

    src = code_only(inspect.getsource(tracing.list_recent_runs))
    assert "_safe_tag" in src, "the graph tag is not matched against the filename"


def test_the_api_pushes_the_graph_filter_down():
    import backend.api.graphs as api

    src = code_only(inspect.getsource(api))
    assert "list_recent_runs ( limit = 200 , graph = graph )" in src.replace("  ", " "), (
        "the API still over-fetches a mixed pool and filters afterwards"
    )


# ---------------------------------------------------------------------------
# #28 / #27 — two halves of one gate disagreeing
# ---------------------------------------------------------------------------

def test_a_non_ascii_dashboard_password_is_decoded_as_utf8():
    """`atob` returns a BYTE string: every UTF-8 byte becomes one character, so
    any non-ASCII password decoded to mojibake and never matched. The operator
    was challenged forever by a password they typed correctly, with no error
    anywhere saying why.

    The proxy already did this correctly with `Buffer.from(...)`, so the two
    halves of one gate disagreed about what the password WAS.
    """
    src = pathlib.Path("middleware.ts").read_text(encoding="utf-8")

    assert "TextDecoder" in src, "the middleware still compares raw bytes as characters"
    assert "TextEncoder" in src, "the comparison still runs over UTF-16 code units"


def test_both_halves_parse_the_open_writes_flag_identically():
    """The backend honoured ALLOW_UNAUTHENTICATED_WRITES and the frontend did
    not, so the documented way out of the fail-closed default did not work end
    to end: the backend accepted writes while the proxy went on refusing them
    with a message about a different variable.

    A security flag meaning "on" to one half and "off" to the other is a hole
    neither side can see.
    """
    import backend.core.auth as auth

    backend_src = inspect.getsource(auth.open_writes_allowed)
    proxy = pathlib.Path("app/api/backend/[...path]/route.ts").read_text(encoding="utf-8")

    assert "openWritesAllowed" in proxy, "the proxy ignores the flag entirely"
    for spelling in ("1", "true", "yes", "on"):
        assert f"'{spelling}'" in proxy or f'"{spelling}"' in proxy, (
            f"the proxy does not accept {spelling!r}"
        )
        assert f'"{spelling}"' in backend_src
