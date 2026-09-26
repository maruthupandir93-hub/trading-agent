"""A model that NEVER ANSWERS is as dead as one returning 410 — and was not detected.

THE MISS, MEASURED
==================
`openai/gpt-oss-20b` was configured on two tiers of this system on 2026-09-25:
`LLM_MODEL_MECHANICAL` and the `external_consultation` panel. It is still listed
by the provider's own GET /v1/models, and it returned no HTTP status at all —
120s of curl produced nothing, and three consecutive calls through the provider
timed out at the 300s reasoning ceiling.

Every graph run therefore paid up to 60s on the mechanical tier and up to 300s on
the consultation node (a live trace: `external_consultation 300,612.9ms`), and
the live monitoring endpoint reported:

    "llmHealth": {"deadModels": [], "healthy": true, "fallbackRatePct": 18.1}

because a timeout is classified "timeout", and only `model_eol` / `model_not_found`
counted toward DEAD. The detector was written after a model went EOL with an HTTP
410, and it had learned exactly that one shape of death. The whole reason this
module exists — "the reasoning layer degraded silently once and the only reason it
was caught is a human noticing the lessons all read the same" — applied again, to
the detector itself.

WHY TIMEOUTS NEED A DIFFERENT TEST FROM A 410
=============================================
A 410 is unambiguous: the endpoint is saying the model is gone. A timeout is not —
a genuinely slow model under load times out sometimes and answers the rest of the
time, and flagging that as dead would send the operator to change a model id when
what they need is a longer tier timeout. So a model is UNRESPONSIVE only when it
has timed out enough times in the window AND has not succeeded once in it. One
success is enough to say "slow, not gone".
"""

from __future__ import annotations

import pytest

from backend.llm.health import (
    _UNRESPONSIVE_THRESHOLD,
    LLMHealthTracker,
)

DEAD = "openai/gpt-oss-20b"
ALIVE = "mistralai/mistral-nemotron"


def _timeouts(tracker: LLMHealthTracker, model: str, n: int) -> None:
    for _ in range(n):
        tracker.record(model=model, tier="reasoning", ok=False,
                       error_class="timeout", error="timed out after 300s")


def _successes(tracker: LLMHealthTracker, model: str, n: int) -> None:
    for _ in range(n):
        tracker.record(model=model, tier="reasoning", ok=True)


def test_a_model_that_only_ever_times_out_is_flagged(monkeypatch):
    """The exact live case this missed."""
    h = LLMHealthTracker()
    _timeouts(h, DEAD, _UNRESPONSIVE_THRESHOLD)
    _successes(h, ALIVE, 5)

    snap = h.snapshot()
    flagged = {d["model"] for d in snap["deadModels"]}
    assert DEAD in flagged
    assert ALIVE not in flagged
    assert snap["healthy"] is False


def test_a_slow_but_working_model_is_not_flagged():
    """The distinction that matters. A model that answers sometimes needs a longer
    tier timeout, not a new model id — and telling the operator to replace it
    would send them to fix the wrong thing."""
    h = LLMHealthTracker()
    _timeouts(h, ALIVE, _UNRESPONSIVE_THRESHOLD + 5)
    _successes(h, ALIVE, 1)          # one success is enough to say "slow, not gone"

    assert [d for d in h.snapshot()["deadModels"] if d["model"] == ALIVE] == []


def test_one_or_two_timeouts_are_not_enough():
    """A higher bar than the 410 rule, because a timeout is ambiguous where a 410
    is not. A transient network stall must not rename a working model as dead."""
    h = LLMHealthTracker()
    _timeouts(h, DEAD, _UNRESPONSIVE_THRESHOLD - 1)

    assert h.snapshot()["deadModels"] == []


def test_the_eol_rule_still_works_and_is_not_duplicated():
    """The original detection is untouched, and a model failing BOTH ways is
    reported once rather than twice."""
    h = LLMHealthTracker()
    for _ in range(2):
        h.record(model=DEAD, tier="reasoning", ok=False,
                 error_class="model_eol", error="HTTP 410 Gone")
    _timeouts(h, DEAD, _UNRESPONSIVE_THRESHOLD)

    entries = [d for d in h.snapshot()["deadModels"] if d["model"] == DEAD]
    assert len(entries) == 1
    assert "EOL" in entries[0]["reason"] or "not-found" in entries[0]["reason"]


def test_the_note_reports_the_ACTUAL_reason():
    """The note used to assert "returning end-of-life / not-found responses" for
    every flagged model. That is flatly wrong for one that is timing out, and a
    confident wrong diagnosis sends the operator to check a status code that
    never arrived."""
    h = LLMHealthTracker()
    _timeouts(h, DEAD, _UNRESPONSIVE_THRESHOLD)

    note = h.snapshot()["note"]
    assert DEAD in note
    assert "timeout" in note.lower()
    assert "not-found" not in note


def test_a_recovered_model_ages_out_of_the_window():
    """Same property the EOL rule has: the window is "recent behaviour", so a
    model that was replaced and is answering again must stop being reported."""
    h = LLMHealthTracker()
    _timeouts(h, DEAD, _UNRESPONSIVE_THRESHOLD)
    assert [d for d in h.snapshot()["deadModels"] if d["model"] == DEAD]

    _successes(h, DEAD, 1)
    assert [d for d in h.snapshot()["deadModels"] if d["model"] == DEAD] == []


def test_an_unattributed_timeout_is_not_blamed_on_a_model():
    """A failure recorded with no model name cannot be pinned on one, and
    inventing an attribution here would name an innocent model in the operator's
    one health readout (invariant 6)."""
    h = LLMHealthTracker()
    for _ in range(_UNRESPONSIVE_THRESHOLD + 2):
        h.record(model=None, tier="reasoning", ok=False,
                 error_class="timeout", error="timed out")

    assert h.snapshot()["deadModels"] == []
