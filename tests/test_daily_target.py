"""Daily profit target — 'take 1-2% a day, over many trades, then stop for the day'.

`_daily_target_reached` gates OPENING new positions once the day is up by the
configured fraction, resuming the next UTC day. It never touches an exit (invariant
4), and it anchors to the day's starting equity so the percentage is per-day.
"""

import datetime

from backend.services.trading_session import TradingSession, _daily_target_reached


def _session(daily_target_pct=None):
    return TradingSession(
        id="dt-test", symbol="SOL/USDT", leverage=5, start_equity=10_000.0,
        target_equity=12_000.0, floor_equity=5_000.0, daily_target_pct=daily_target_pct,
    )


def test_no_daily_target_never_locks():
    s = _session(daily_target_pct=None)
    assert _daily_target_reached(s, 11_000.0) is False


def test_first_call_anchors_the_day_and_does_not_lock():
    s = _session(daily_target_pct=0.02)
    assert _daily_target_reached(s, 10_000.0) is False
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    assert s.day_anchor_date == today
    assert s.day_anchor_equity == 10_000.0


def test_below_target_does_not_lock_but_at_target_does():
    s = _session(daily_target_pct=0.02)
    _daily_target_reached(s, 10_000.0)          # anchor at 10k
    assert _daily_target_reached(s, 10_150.0) is False   # +1.5% — keep trading
    assert _daily_target_reached(s, 10_200.0) is True    # +2.0% — bank the day


def test_a_new_utc_day_resets_the_anchor_and_unlocks():
    s = _session(daily_target_pct=0.02)
    _daily_target_reached(s, 10_000.0)          # anchor today
    assert _daily_target_reached(s, 10_200.0) is True    # locked today
    # Simulate the UTC rollover: yesterday's anchor is stale.
    s.day_anchor_date = "2000-01-01"
    # Same equity, but it is a "new day" now -> re-anchors, does not lock.
    assert _daily_target_reached(s, 10_200.0) is False
    assert s.day_anchor_equity == 10_200.0
