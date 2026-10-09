"""The approved direction is a Literal, because a loose string is a REVERSED trade.

HOW THIS WAS FOUND. A full-system harness built a `TarApprovedEvent` with
`direction="long"`, drove it through the real bus and the real execution agent,
and the paper book came back holding:

    {"symbol": "SOL/USDT", "qty": 1.0, "avgCost": 100.0, "side": "sell"}

A LONG thesis opened as a SHORT. `execution_agent` derives the side with an
exact, case-sensitive comparison --

    side = "buy" if tar.direction == "LONG" else "sell"

-- so anything that is not exactly "LONG" falls to the else. That is not a
failed trade, it is the OPPOSITE trade: against the direction the panel
approved, with the stop sitting above the entry where the target belongs, and
the monitor then enforcing a stop on a position facing the wrong way.

NO LIVE TRADE HAS EVER BEEN REVERSED BY THIS, and saying so matters as much as
the fix. Both producers emit uppercase: `execution_service` writes
`"LONG" if event.side == "buy" else "SHORT"`, and `supervisor_agent` refuses
outright unless the debate's direction is in ("LONG", "SHORT"). The exposure
was a FUTURE producer -- and a present one in this suite.

SEVEN TEST FILES BUILT THE EVENT WITH LOWERCASE: test_partial_tp,
test_profit_target, test_trailing_stop, test_excursion, test_resting_stop_mode,
test_post_trade_chain and test_position_persistence. Every one was exercising
the SELL branch whatever its fixture said it was setting up, so the LONG path
through `_execute_tar` was less covered than a green suite suggested. They all
still pass uppercased -- they derived their expectations from the `side` they
passed separately, so they were internally consistent -- but they were
constructing an event production cannot produce, and this codebase already has
the rule for that: a fixture that differs from the real thing proves nothing
about the real thing.

`TarSubmittedEvent` one class up has always been `Literal['LONG', 'SHORT']`,
and `tab` inside this very class is a Literal. This field was the odd one out,
and it was the one field whose looseness inverts a trade.
"""

from __future__ import annotations

import inspect
import re
import uuid

import pytest
from pydantic import ValidationError

from backend.models.events import TarApprovedEvent, TarSubmittedEvent


def _approved(direction):
    return TarApprovedEvent(
        tar_id=uuid.uuid4(),
        symbol="SOL/USDT",
        direction=direction,
        approved_size=1.0,
        approved_leverage=5,
        cro_rationale="test",
        stop_loss=95.0,
        tab="paper",
    )


def test_the_two_production_values_are_accepted():
    assert _approved("LONG").direction == "LONG"
    assert _approved("SHORT").direction == "SHORT"


@pytest.mark.parametrize("bad", ["long", "short", "Long", "buy", "sell", "NEUTRAL", ""])
def test_anything_else_is_refused_at_construction(bad):
    """LOUDLY, at the boundary. The alternative is not an error anywhere -- it
    is a silently inverted position that looks completely normal in the book,
    in the watch list and on the dashboard."""
    with pytest.raises(ValidationError):
        _approved(bad)


def test_it_matches_the_event_one_hop_earlier():
    """`cro_agent` does `direction=tar.direction` straight from a
    `TarSubmittedEvent`. Two adjacent events in one chain disagreeing about
    what a direction may be is how a value widens on the way through."""
    submitted = TarSubmittedEvent.model_fields["direction"].annotation
    approved = TarApprovedEvent.model_fields["direction"].annotation
    assert submitted == approved


def test_the_executor_still_compares_against_the_literal_this_guards():
    """IF THE COMPARISON MOVES, THIS TEST IS GUARDING NOTHING.

    The Literal is only load-bearing because `execution_agent` reads the field
    with an exact `== "LONG"`. A future rewrite to `.upper()` or to an enum
    would make the restriction harmless -- and would also make this file's
    reasoning wrong, which is worth finding out from a failure rather than
    from a reversed position.
    """
    from backend.agents import execution_agent

    src = inspect.getsource(execution_agent)
    assert re.search(r'side\s*=\s*"buy"\s+if\s+tar\.direction\s*==\s*"LONG"', src), (
        "the side derivation moved; re-check whether the Literal still protects it"
    )


def test_no_test_in_this_suite_builds_the_event_with_a_lowercase_direction():
    """The seven files are fixed; this stops the eighth.

    Scanned as source rather than by driving every test, because the point is
    that such a fixture must not be WRITTEN -- by the time it runs it has
    already silently changed which branch its file covers.
    """
    from pathlib import Path

    offenders = []
    tests_dir = Path(__file__).resolve().parent
    pattern = re.compile(r'direction\s*=\s*["\'](?!LONG|SHORT)(long|short|Long|Short)["\']')
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Only where a TarApprovedEvent is being built. `direction="long"` is a
        # perfectly good value on other objects -- `_Tracked`, the trade row and
        # the backtest's Trade all use lowercase -- so an unscoped scan would
        # flag honest code and teach the next reader to ignore this test.
        for match in re.finditer(r"TarApprovedEvent\(", text):
            window = text[match.start(): match.start() + 600]
            if pattern.search(window):
                offenders.append(path.name)
                break
    assert not offenders, (
        f"these files build TarApprovedEvent with a lowercase direction and are "
        f"therefore testing the SELL branch whatever they say: {offenders}"
    )
