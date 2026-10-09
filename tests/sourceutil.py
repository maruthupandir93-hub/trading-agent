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


def code_only(src: str) -> str:
    """`src` with comments and string literals removed.

    The result is token text joined by spaces, so it is suitable for substring
    checks and NOT for anything that cares about layout.
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)
