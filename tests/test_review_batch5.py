"""Batch 5: the three findings with real substance, and one honest relabel.

Issue 3, Issue 8 and Issue 10 all come down to the same question — does the
system's own report of what it is doing match what it is doing? A position
labelled protected that the venue refused a stop for; a duplicate guard that a
restart erases; a log announcing a risk control that never runs.
"""

from __future__ import annotations

import inspect

import pytest

from tests.sourceutil import code_only


# ---------------------------------------------------------------------------
# Issue 3 — protection as an explicit state
# ---------------------------------------------------------------------------

def _tracked(**kw):
    from backend.agents.position_monitor import _Tracked

    base = dict(tar_id="t1", symbol="SOL/USDT", side="buy", tab="real", qty=5.0)
    base.update(kw)
    return _Tracked(**base)


def test_the_four_protection_states_are_distinct():
    """`stop_order_id is None` was written by THREE different outcomes an
    operator must tell apart — never attempted, attempted and REFUSED, and a
    venue that cannot rest orders — and the dashboard rendered the stop level
    in every case, so all three read as "protected"."""
    import backend.agents.position_monitor as pm

    states = {
        pm.PROTECTION_VENUE,
        pm.PROTECTION_IN_PROCESS,
        pm.PROTECTION_PARTIAL,
        pm.PROTECTION_UNPROTECTED,
    }
    assert len(states) == 4, "two protection states collide"


def test_a_position_starts_in_process_not_unknown():
    """None would read as "unknown" on a paper position whose stop genuinely
    IS enforced in-process — the ambiguity this field exists to remove."""
    from backend.agents.position_monitor import PROTECTION_IN_PROCESS

    pos = _tracked()
    assert pos.protection == PROTECTION_IN_PROCESS
    assert pos.protected_qty == 0.0


def test_protection_is_surfaced_so_a_reader_need_not_infer_it():
    import backend.agents.position_monitor as pm

    src = code_only(inspect.getsource(pm.PositionMonitorAgent.snapshot_open))
    assert "protection" in src and "protected_qty" in src, (
        "the API still reports only the stop LEVEL, which three different "
        "outcomes all produce"
    )


@pytest.mark.parametrize("reported,requested,expected_full", [
    (5.0, 5.0, True),        # exact
    (4.999, 5.0, True),      # a step-size trim is not a gap worth alarming on
    (2.5, 5.0, False),       # half the position has nothing behind it
    (None, 5.0, True),       # the venue did not say — not the same as zero
])
def test_the_resting_quantity_decides_whether_it_counts_as_protected(reported, requested, expected_full):
    """The review's requirement: do not label it protected until the venue
    confirms the stop order AND ITS QUANTITY.

    A venue can accept a reduce-only stop for LESS than the position. The order
    exists, `result.ok` is true, and the uncovered remainder has no
    exchange-side protection while the system reports it as protected.
    """
    from backend.agents.position_monitor import PROTECTION_QTY_TOLERANCE, _resting_qty

    class R:
        raw = {"amount": reported} if reported is not None else {}

    covered = _resting_qty(R(), requested)
    if reported is None:
        assert covered is None, "an unstated quantity must not read as zero"
        return
    is_full = covered >= requested * PROTECTION_QTY_TOLERANCE
    assert is_full is expected_full


def test_a_refused_stop_is_recorded_as_unprotected():
    """The one state that must never be read as protected."""
    import backend.agents.position_monitor as pm

    src = code_only(inspect.getsource(pm.PositionMonitorAgent._place_resting_stop))
    assert "PROTECTION_UNPROTECTED" in src
    assert "PROTECTION_PARTIAL" in src
    assert "PROTECTION_VENUE" in src


# ---------------------------------------------------------------------------
# Issue 8 — the duplicate guard must survive a restart
# ---------------------------------------------------------------------------

def test_the_idempotency_basis_is_claimed_durably():
    """`_submitted` is an in-process dict, so a plan whose TAR was published
    immediately before a crash could be submitted AGAIN after one — the same
    "one decision, two positions at full size" outcome the concurrent-claim fix
    already closed, reached by a restart instead."""
    import backend.services.execution_service as es

    src = code_only(inspect.getsource(es))
    assert "_claim_basis_durably" in src
    assert "execution_submissions" in inspect.getsource(es)


def test_the_durable_claim_runs_AFTER_the_in_memory_one():
    """Ordering is the whole guarantee. The durable claim awaits, and anything
    that awaits before the memory claim reopens the concurrency hole that fix
    closed — two tasks carrying one plan both passing the duplicate test."""
    import backend.services.execution_service as es

    src = inspect.getsource(es.ExecutionService._handle)
    memory = src.index("_submitted[basis] = receipt")
    durable = src.index("_claim_basis_durably")
    assert memory < durable, "the durable claim awaits before the memory claim"


def test_a_close_is_never_claimed_durably():
    """`_close` withholds its basis on a wiring failure precisely so the exit
    stays retryable (invariant 4). A durable claim would make that permanent:
    the retry refused as a duplicate while the position stayed open."""
    import backend.services.execution_service as es

    src = inspect.getsource(es.ExecutionService._handle)
    block = src[src.index("_claim_basis_durably") - 400: src.index("_claim_basis_durably") + 200]
    assert 'intent != "close"' in block


@pytest.mark.asyncio
async def test_no_database_is_not_a_refusal():
    """Refusing to trade because a key store is unreachable would turn a
    bookkeeping outage into a trading one. The in-memory guard still covers the
    common case."""
    import backend.services.execution_service as es

    assert await es._claim_basis_durably(object(), "some-basis") is True


def test_the_table_is_idempotent_and_keyed_on_the_basis():
    """`schema.sql` is applied on EVERY startup, and the PRIMARY KEY is what
    makes the claim atomic — the database decides, not an ordering argument."""
    import pathlib

    schema = pathlib.Path("db/schema.sql").read_text(encoding="utf-8")
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS execution_submissions"):]
    block = block[: block.index(");")]
    assert "basis       text PRIMARY KEY" in block


# ---------------------------------------------------------------------------
# Issue 10 — a log that announced a control that never ran
# ---------------------------------------------------------------------------

def test_the_twap_log_does_not_claim_an_active_control():
    """The slices are computed and NOT executed — a single market order for the
    full size is placed, and there is no scheduler anywhere.

    This is the SECOND time this file claimed an inactive TWAP: the first
    logged "Engaging TWAP execution logic" on a bare size threshold "while
    doing nothing at all". Computing real slices and still not placing them is
    the same claim with better arithmetic behind it.
    """
    import backend.agents.execution_agent as ea

    raw = inspect.getsource(ea.ExecutionAgent._execute_tar)
    assert "NOT APPLIED" in raw, "the log still states that slicing happened"

    # STRIPPED, because the comment deliberately QUOTES the old line as the
    # finding — the fifth time a source-scanning test in this suite has first
    # failed on the explanation rather than the code. `code_only` also removes
    # the string literals, so the check is that no *format call* survives with
    # the old shape rather than a substring hunt.
    code = code_only(raw)
    assert "slices" in code, "the slicing computation was removed, not relabelled"
    assert "NOT APPLIED" not in code, (
        "sanity: the new wording lives in a string literal, which code_only strips"
    )


def test_the_agents_declared_purpose_matches_what_it_does():
    """`/api/agents` renders this. It claimed "order splitting (TWAP/VWAP)" as
    a capability."""
    from backend.agents.execution_agent import ExecutionAgent

    purpose = ExecutionAgent().purpose
    assert "NOT APPLIED" in purpose
    assert "depth cap" in purpose, (
        "the purpose should name what DOES bound impact, or a reader concludes "
        "nothing does"
    )
