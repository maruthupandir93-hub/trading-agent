"""Helpers for tests that assert things about this project's own source.

Several tests here guard a property by reading the code rather than driving it
-- that a module names exactly one host, that a pre-check does not re-derive a
venue rule, that a dead config key is gone. Those assertions keep failing on
the EXPLANATION rather than the code, because a comment that warns about a
thing has to name it:

    test_binance_testnet_mirror   failed on `api.binance.com` in the docstring
                                  that records the mainnet host as the finding
    test_session_venue_minimum    failed on MIN_NOTIONAL in the table of
                                  measured minimums
    test_no_decision_reason       failed on `noDecisionReason` in the comment
                                  explaining why that key is dead

A mention is not a call, and the explanation has to keep naming what it warns
about -- so the answer is always to strip comments and strings, never to
water down the comment. `tokenize` rather than a regex, because a regex that
tries to find string literals in Python gets nested quotes and f-strings wrong
in exactly the cases that matter.

This lived in three test files before it lived here.
"""

from __future__ import annotations

import io
import tokenize


# F-STRINGS ARE NOT `STRING` TOKENS ON PYTHON 3.12+, AND THAT MADE THIS
# HELPER QUIETLY WEAKER THAN EVERY TEST USING IT ASSUMED.
#
# PEP 701 re-tokenised f-strings into FSTRING_START / FSTRING_MIDDLE /
# FSTRING_END, so `tokenize.STRING` no longer matches their contents. Every
# source-scanning test in this suite therefore kept seeing the inside of every
# f-string — which is exactly what these tests exist to ignore, because a
# MENTION is not a call.
#
# Found when a retention test asserted `t.tar_id` was gone from the module and
# failed on the SQL COMMENT inside an f-string that NAMES the dead column as
# the finding. That is the fourth time a source-scanning test in this project
# has failed on the explanation rather than the code, and the first time the
# cause was the stripper itself rather than the comment.
#
# `getattr` for the names, because they do not exist before 3.12 and this must
# keep working on both.
_FSTRING_PARTS = tuple(
    t for t in (
        getattr(tokenize, "FSTRING_START", None),
        getattr(tokenize, "FSTRING_MIDDLE", None),
        getattr(tokenize, "FSTRING_END", None),
    )
    if t is not None
)

_STRIPPED = (tokenize.COMMENT, tokenize.STRING) + _FSTRING_PARTS


def code_only(src: str) -> str:
    """`src` with comments and string literals — f-strings included — removed.

    The result is token text joined by spaces, so it is suitable for substring
    checks and NOT for anything that cares about layout.

    AN f-STRING'S INTERPOLATIONS SURVIVE, deliberately. `f"{build_url(host)}"`
    really does CALL `build_url`, so a test asking "does this module call X"
    must still see it; only the literal text between the braces is dropped.
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in _STRIPPED:
            continue
        out.append(tok.string)
    return " ".join(out)
