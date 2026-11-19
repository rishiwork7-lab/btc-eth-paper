import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine  # noqa: E402
from scalper.futures import DipBuyer, exit_due, signal  # noqa: E402
from test_engine import PRODUCTS, drain  # noqa: E402

DAY0 = 1794528000  # 2026-11-13 00:00 UTC


def series(last):
    """200 rising closes, then the given last closes."""
    return [1000 + i for i in range(200)] + last


def test_signal_needs_uptrend_and_three_lower_closes():
    assert signal(series([1300, 1290, 1280, 1270]))["ok"]                 # above the 200-day average, 3 lower closes
    assert not signal(series([1300, 1290, 1295, 1270]))["ok"]             # streak broken
    assert not signal(series([1300, 1000, 900, 800]))["ok"]               # 3 lower closes but below the 200-day average
    assert not signal([1000.0] * 50)["ok"]                                # not enough history


def test_exit_on_bounce_or_after_ten_days():
    assert exit_due([1260], series([1300, 1290, 1280, 1270, 1260])) is None          # still falling: hold
    assert "bounce" in (exit_due([1300], series([1270, 1260, 1250, 1240, 1300])) or "")
    assert "time stop" in (exit_due([1200] * 10, series([1300, 1290, 1280, 1270, 1260])) or "")


def test_a_full_paper_trade(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    eng = Engine(tmp_path, PRODUCTS)
    i = eng.inst["ETHUSD"]
    closes = {"v": series([1300, 1290, 1280, 1270])}

    def fetch(url):
        if "FUNDING" in url:
            return {"result": [{"time": DAY0 + 28800, "close": 0.01}]}
        c, end = closes["v"], int(url.split("end=")[1].split("&")[0])      # the last close is the day before `end`
        return {"result": [{"time": end - 86400 * (len(c) - j), "close": x} for j, x in enumerate(c)]}
    b = DipBuyer(eng, 10000.0, fetch=fetch)
    real = time.time
    try:
        time.time = lambda: DAY0 + 200
        i.bid, i.ask, i.l1_rx = 1269.5, 1270.0, DAY0 + 199
        b.step(DAY0 + 200)
        p = b.state["pos"]
        assert p and p["entry_px"] == 1270.0 and p["qty"] == 7.87           # bought at the ask, 1x notional
        closes["v"] = closes["v"] + [1310.0]                                  # next day closes above the 5-day average
        time.time = lambda: DAY0 + 86400 + 200
        i.bid, i.ask, i.l1_rx = 1310.0, 1310.5, DAY0 + 86400 + 199
        b.step(DAY0 + 86400 + 200)
    finally:
        time.time = real
    assert b.state["pos"] is None and b.state["cash"] > 10000 + 7.87 * 39 - 15   # sold at the bid, fees and funding paid
    drain(eng)
