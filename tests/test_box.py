import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from box_backtest import run  # noqa: E402

T0 = 1_795_000_000 // 60 * 60


def series(after):
    """1,500 wide candles (a normal day), then 30 tight ones (the box 1000-1002), then the given candles."""
    bars, t = [], T0
    for i in range(1500):
        lo = 990 + (i % 7) * 2
        bars.append([t, lo + 5, lo + 12, lo, lo + 6, 1.0]); t += 60
    for i in range(30):
        bars.append([t, 1001, 1002, 1000, 1001, 1.0]); t += 60
    for o, h, l, c in after:
        bars.append([t, o, h, l, c, 1.0]); t += 60
    return bars


def test_box_breakout_up_enters_at_the_top_and_hits_a_1_to_1_target():
    b = series([(1001.5, 1002.5, 1001.5, 1002.4),    # breaks the top 1002: long at 1002, stop 1000, target 1004
                (1002.4, 1004.5, 1002.2, 1004.3)])   # trades through 1004: target
    r = run("ETHUSD", b, 30, 0.5, "touch", False, 1)
    assert r["trades"] == 1 and r["outcomes"]["target"] == 1
    assert r["gross_r"]["avg_bp"] == pytest.approx(1.0, abs=1e-6)          # exactly 1R before costs
    assert r["net_r"]["avg_bp"] < 1.0


def test_stop_counts_first_when_one_candle_touches_both():
    b = series([(1001.5, 1002.5, 1001.5, 1002.4), (1002.4, 1004.5, 999.5, 1001)])
    r = run("ETHUSD", b, 30, 0.5, "touch", False, 1)
    assert r["outcomes"]["stop"] == 1 and r["gross_r"]["avg_bp"] == pytest.approx(-1.0, abs=1e-6)


def test_a_wide_box_is_not_a_consolidation():
    b = series([])[:1500] + [[T0 + 60 * (1500 + i), 1000, 1015, 985, 1000, 1.0] for i in range(30)] + [[T0 + 60 * 1530, 1001, 1020, 1000, 1019, 1.0]]
    assert run("ETHUSD", b, 30, 0.5, "touch", False, 1)["trades"] == 0
