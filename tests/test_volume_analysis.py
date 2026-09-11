"""Volume analysis — RVOL and sudden-surge detection.

The operator's ask: a slight increase in a candle's volume tends to precede a move,
and a sudden spike should be ACTED on, not averaged away. These tests pin that a
single heavy candle is detected (where a 5-bar average would dilute it), that the
direction always comes from the price move (never invented from volume), and that
the surge actually strengthens the debate's Volume argument.
"""

from backend.algorithms.volume_analysis import analyze_volume, SURGE_RVOL


def _bars(volumes, closes):
    """Build candles from parallel volume/close lists (opens/highs/lows derived)."""
    out = []
    for i, (v, c) in enumerate(zip(volumes, closes)):
        prev = closes[i - 1] if i else c
        out.append({
            "time": i, "open": prev, "high": max(prev, c) + 1,
            "low": min(prev, c) - 1, "close": c, "volume": v,
        })
    return out


def test_too_few_candles_is_unavailable():
    v = analyze_volume(_bars([100] * 5, [10] * 5))
    assert v.available is False


def test_all_zero_volume_is_unavailable_not_neutral():
    v = analyze_volume(_bars([0.0] * 30, [10.0] * 30))
    assert v.available is False  # a missing signal, never a neutral confirmation


def test_a_sudden_surge_on_an_up_candle_is_bullish():
    # 25 flat bars at volume 100, price drifting up; last bar 3x volume, big up move.
    vols = [100.0] * 24 + [300.0]
    closes = [10.0 + i * 0.01 for i in range(24)] + [10.5]
    v = analyze_volume(_bars(vols, closes))
    assert v.available is True
    assert v.rvol == 300.0 / 100.0
    assert v.surge is True
    assert v.direction == "bullish"
    assert v.strength > 0  # bullish confirmation


def test_a_sudden_surge_on_a_down_candle_is_bearish():
    vols = [100.0] * 24 + [300.0]
    closes = [10.0] * 24 + [9.5]  # last candle drops hard on heavy volume
    v = analyze_volume(_bars(vols, closes))
    assert v.surge is True
    assert v.direction == "bearish"
    assert v.strength < 0  # heavy SELLING must not read as bullish


def test_normal_volume_is_not_a_surge():
    vols = [100.0] * 24 + [105.0]  # a 1.05x bar — ordinary noise
    closes = [10.0 + i * 0.01 for i in range(25)]
    v = analyze_volume(_bars(vols, closes))
    assert v.rvol < SURGE_RVOL
    assert v.surge is False


def test_a_surge_with_no_price_move_contributes_nothing():
    vols = [100.0] * 24 + [400.0]
    closes = [10.0] * 25  # heavy volume, price unchanged = indecision
    v = analyze_volume(_bars(vols, closes))
    assert v.surge is True
    assert v.strength == 0.0  # no move => no confirmation


def test_the_debate_volume_argument_reacts_to_a_sudden_surge():
    """A latest-candle surge in the move's direction must strengthen the Volume
    leg beyond what the quiet 5/20 baseline alone would give."""
    from tests.conftest import make_candles
    from backend.algorithms.debate import score_debate

    base = make_candles(120)

    def volume_arg(result):
        for a in result.bull_arguments + result.bear_arguments:
            if a.name == "Volume":
                return a
        return None

    calm = volume_arg(score_debate(base))

    # Same candles, but the LAST bar carries a big volume surge on an up move.
    surged = [dict(c) for c in base]
    surged[-1] = {**surged[-1], "volume": surged[-1]["volume"] * 5,
                  "close": surged[-1]["close"] * 1.01, "high": surged[-1]["high"] * 1.02}
    hot = volume_arg(score_debate(surged))

    assert hot is not None
    # The surge candle produces a stronger (larger-magnitude) volume confirmation
    # than the un-surged baseline, and says so.
    assert abs(hot.score) >= abs(calm.score) if calm else abs(hot.score) > 0
    assert "SURGE" in hot.detail
