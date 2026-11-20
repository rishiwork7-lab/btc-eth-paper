import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from breakout_backtest import resample, run  # noqa: E402

T = 1_795_000_000 // 300 * 300


def bars(rows):
    return [[T + 60 * i, o, h, l, c, 1.0] for i, (o, h, l, c) in enumerate(rows)]


def test_long_and_short_breakouts_exit_at_the_next_close():
    b = bars([(100, 101, 99, 100),
              (100, 102, 99.5, 101.5),    # breaks 101 -> long at 101.05, exit at the next close 102
              (101.5, 103, 101, 102),     # in the trade: no new signal
              (102, 102.5, 100.5, 101),   # breaks the previous low 101 -> short at 100.95
              (101, 101.2, 99, 99.5),     # exit at 99.5
              (99.5, 99.6, 99.4, 99.5)])
    r = run("ETHUSD", b, 1, "next")
    assert r["trades"] == 2 and r["long"]["n"] == 1 and r["short"]["n"] == 1
    assert r["long"]["avg_bp"] == pytest.approx((102 * (1 - 1.125e-4)) / (101.05 * (1 + 1.125e-4)) * 1e4 - 1e4 - 11.8, abs=0.01)
    assert r["gross"]["avg_bp"] == pytest.approx(((102 / 101.05 - 1) + (1 - 99.5 / 100.95)) / 2 * 1e4, abs=0.01)


def test_a_candle_breaking_both_levels_is_skipped_and_gaps_fill_at_the_open():
    b = bars([(100, 101, 99, 100), (100, 102, 98, 100), (100, 100.5, 99.8, 100), (103, 104, 102.9, 103.5), (103.5, 104, 103, 103.8)])
    r = run("ETHUSD", b, 1, "next")
    assert r["skipped_both"] == 1
    assert r["trades"] == 1 and r["gross"]["avg_bp"] == pytest.approx((103.8 / 103 - 1) * 1e4, abs=0.01)   # gap up: filled at the open 103


def test_five_minute_candles_are_built_from_whole_five_minute_blocks():
    one = bars([(i, i + 1, i - 1, i + 0.5) for i in range(100, 112)])     # 12 one-minute candles
    five = resample(one, 5)
    assert len(five) == 2 and five[0][1] == 100 and five[0][4] == 104.5 and five[0][2] == 105 and five[1][3] == 104
