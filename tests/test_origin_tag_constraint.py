"""Every `origin_tag` the code writes must be permitted by the schema's CHECK.

WHY THIS TEST EXISTS — IT COST EVERY CLOSING TRADE IN THE DATABASE
==================================================================
`trades_origin_tag_check` listed five tags. The code writes SEVEN. The two
missing ones were the two that mattered most:

    agent-close    `position_monitor._persist_closed_trade` — the ONLY writer of
                   a row carrying a realized `pnl`
    manual-panel   `api/operator_trade` — a manual paper trade

A CHECK constraint that omits a value the code emits does not degrade. It
REJECTS the INSERT:

    new row for relation "trades" violates check constraint
    "trades_origin_tag_check"

That was logged at ERROR and swallowed — correctly, because by then the position
was already closed and the money had moved, and raising would have made the
caller retry an exit for a flat position. So every close worked and every RECORD
of a close was lost.

The visible symptom was three pages away and looked like arithmetic: `trades`
contained nothing but opening fills, `realised()` found no row carrying a pnl,
and the P&L panel, win rate, expectancy and max drawdown were all blank on an
account that had been trading.

`CREATE TABLE IF NOT EXISTS` cannot widen a CHECK on a live table, so the fix had
to DROP and re-ADD it inside a DO block. This test compares the two sides — what
the code emits, and what the schema permits — so the next omission fails here
instead of silently discarding trades.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The Python modules that INSERT into `trades`. Listed explicitly rather than
# discovered, so adding a fifth writer is a deliberate act that shows up here.
WRITERS = (
    "backend/agents/execution_agent.py",
    "backend/agents/position_monitor.py",
    "backend/agents/supervisor_agent.py",
    "backend/api/operator_trade.py",
    "backend/api/operator_exchange.py",
)

_TAG_RE = re.compile(r"""["']((?:agent|manual|user|chat)-[a-z-]+|debate)["']""")

# Only literals inside an `INSERT INTO trades` statement, or on a line that names
# `origin_tag`, are origin tags.
#
# A broader scan is wrong and this test proved it on its first run: it flagged
# `manual-position-tracked`, which is a `record_decision` KIND, not an origin tag.
# Hyphenated string literals are everywhere in this codebase, so the tag has to
# be identified by WHERE it is used rather than by what it looks like.
_INSERT_WINDOW = 30


def _schema_allowed() -> set[str]:
    """The tags the live schema's CHECK permits, read from schema.sql itself."""
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    # The DO block is the authority on an existing database — the CREATE block
    # only applies to a fresh one, and the two are kept in sync by hand.
    block = re.search(
        r"ADD CONSTRAINT trades_origin_tag_check\s*\n?\s*CHECK \(origin_tag IN \(([^)]*)\)\)",
        sql,
    )
    assert block, "the trades_origin_tag_check DO block is missing from schema.sql"
    return set(re.findall(r"'([^']+)'", block.group(1)))


def _emitted() -> dict[str, list[str]]:
    """Tag -> the writers that emit it."""
    found: dict[str, list[str]] = {}
    for rel in WRITERS:
        path = ROOT / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()

        # Line numbers that belong to a trades INSERT, plus its parameter list.
        in_scope: set[int] = set()
        for i, line in enumerate(lines):
            if "INSERT INTO trades" in line:
                in_scope.update(range(i, min(i + _INSERT_WINDOW, len(lines))))
            if "origin_tag" in line:
                in_scope.add(i)

        for i in sorted(in_scope):
            stripped = lines[i].strip()
            # Comments are skipped, which is what keeps the docstrings that
            # EXPLAIN this bug from reading as emitted tags.
            if stripped.startswith("#") or stripped.startswith("*") or stripped.startswith("--"):
                continue
            for tag in _TAG_RE.findall(lines[i]):
                found.setdefault(tag, []).append(rel)
    return found


def test_the_schema_permits_every_tag_the_code_writes():
    allowed = _schema_allowed()
    emitted = _emitted()

    missing = {tag: writers for tag, writers in emitted.items() if tag not in allowed}
    assert not missing, (
        "these origin_tag values are written by the code but REJECTED by the "
        f"schema's CHECK, so every such INSERT is discarded: {missing}. "
        f"Schema allows: {sorted(allowed)}"
    )


@pytest.mark.parametrize("tag", ["agent-close", "manual-panel"])
def test_the_two_tags_that_were_missing_are_permitted(tag):
    """Named explicitly, because these are the ones that were lost.

    `agent-close` is the only writer of a realized `pnl`, so its rejection is
    what blanked the entire P&L dashboard.
    """
    assert tag in _schema_allowed()


def test_the_create_block_and_the_constraint_block_agree():
    """Both exist and serve different starting states — a fresh database uses the
    CREATE, an existing one uses the DO block. Drifting apart means a new install
    and an upgraded one enforce different rules."""
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    create = re.search(
        r"origin_tag\s+text CHECK \(origin_tag IN \(([^)]*)\)\)", sql
    )
    assert create, "the trades CREATE block's origin_tag CHECK is missing"
    assert set(re.findall(r"'([^']+)'", create.group(1))) == _schema_allowed()


def test_the_typescript_union_matches_the_schema():
    """`TradeLogEntry['originTag']` is the frontend's copy of the same list.

    It was ALSO missing these two, so real trades grouped under a tag TypeScript
    had no name for. Three copies of one list is the shape of this problem; this
    asserts they agree rather than pretending there is only one.
    """
    ts = (ROOT / "lib" / "types.ts").read_text(encoding="utf-8")
    block = re.search(r"originTag\?:\s*((?:\s*\|\s*'[^']+')+);", ts)
    assert block, "TradeLogEntry.originTag union not found in lib/types.ts"
    assert set(re.findall(r"'([^']+)'", block.group(1))) == _schema_allowed()
