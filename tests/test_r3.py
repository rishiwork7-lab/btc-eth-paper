import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine  # noqa: E402
from scalper.r3 import COINS15, CarrySleeve, TrendSleeve, carry_targets, trend_targets  # noqa: E402
from test_engine import PRODUCTS, drain  # noqa: E402

DAY0 = 1794528000  # 2026-11-13 00:00 UTC, a Friday


def rising(n=40, start=100.0, step=1.0):
    return [start + step * i + (0.5 if i % 2 else -0.5) for i in range(n)]


def test_trend_targets_risk_parity_and_budget():
    closes = {"BTC": rising(), "ETH": rising(step=-1.0, start=200), "XRP": rising(step=2.0)}
    tgt, info = trend_targets(closes, 10000.0)
    assert info["BTC"]["on"] and not info["ETH"]["on"] and tgt["ETH"] == 0
    assert 0 < sum(tgt.values()) <= 0.21 * 1.5 * 10000 + 1e-6        # gross never above the sleeve budget


def test_carry_shorts_the_crowded_and_longs_the_uncrowded():
    funding = {c: i * 1e-5 for i, c in enumerate(COINS15)}
    vols = {c: 0.5 for c in COINS15}
    tgt, info = carry_targets(funding, vols, 10000.0)
    assert info["shorts"] == COINS15[-5:] and info["longs"] == COINS15[:5]
    assert sum(v for v in tgt.values() if v > 0) == pytest.approx(0.35 * 1.5 * 10000 / 2)
    assert sum(v for v in tgt.values() if v < 0) == pytest.approx(-0.35 * 1.5 * 10000 / 2)


def make(tmp_path, cls, fetch):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    eng = Engine(tmp_path, PRODUCTS)
    return eng, cls(eng, 10000.0, fetch=fetch)


def test_fill_accounting_long_then_reduce_and_flip(tmp_path):
    eng, s = make(tmp_path, TrendSleeve, lambda u: {"result": {"contract_value": "0.01", "taker_commission_rate": "0.0005"}})
    s.fill(DAY0, "ETH", 1.0, {"bid": 99.0, "ask": 100.0})
    s.fill(DAY0, "ETH", -0.5, {"bid": 110.0, "ask": 111.0})           # sell half at the bid: +5 realized
    assert s.state["realized"] == pytest.approx(5.0) and s.state["pos"]["ETH"]["qty"] == pytest.approx(0.5)
    s.fill(DAY0, "ETH", -1.0, {"bid": 90.0, "ask": 91.0})             # close 0.5 (-5) and open 0.5 short at 90
    assert s.state["realized"] == pytest.approx(0.0) and s.state["pos"]["ETH"] == {"qty": -0.5, "avg": 90.0}
    assert s.state["fees"] == pytest.approx((100 + 55 + 90) * 0.0005 * 1.18)
    drain(eng)


def test_a_daily_trend_run(tmp_path):
    def fetch(url):
        if "/v2/products/" in url:
            return {"result": {"contract_value": "1", "taker_commission_rate": "0.0005"}}
        if "/v2/tickers?" in url:
            return {"result": [{"symbol": f"{c}USD", "mark_price": "139.5", "quotes": {"best_bid": "139", "best_ask": "140"}} for c in COINS15]}
        if "FUNDING:" in url:
            return {"result": []}
        if "/v2/history/candles" in url:
            end = int(url.split("end=")[1].split("&")[0])
            xs = rising() if "BTCUSD" in url or "ETHUSD" in url else rising(step=-1.0, start=200)
            return {"result": [{"time": end - 86400 * (len(xs) - j), "close": x} for j, x in enumerate(xs)]}
        raise AssertionError(url)
    eng, s = make(tmp_path, TrendSleeve, fetch)
    s.step(DAY0 + 200)
    assert set(s.state["pos"]) == {"BTC", "ETH"} and all(p["qty"] > 0 for p in s.state["pos"].values())
    assert s.state["last_run"] == "2026-11-13" and s.state["fees"] > 0
    n = len(s.state["events"])
    s.step(DAY0 + 260)                                                 # same day: no second rebalance
    assert len(s.state["events"]) == n
    drain(eng)


def test_carry_waits_for_monday_after_first_start(tmp_path):
    eng, s = make(tmp_path, CarrySleeve, lambda u: {"result": []})
    s.state["last_run"] = "2026-11-09"
    assert s.due(DAY0 + 400) is None                                   # Friday: no rebalance
    assert s.due(DAY0 + 3 * 86400 + 400) == "2026-11-16"               # next Monday
    drain(eng)


def test_be_trend_trades_only_btc_and_eth(tmp_path):
    from scalper.r3 import BETrendSleeve
    def fetch(url):
        if "/v2/products/" in url:
            return {"result": {"contract_value": "0.001", "taker_commission_rate": "0.0005"}}
        if "/v2/tickers?" in url:
            return {"result": [{"symbol": f"{c}USD", "mark_price": "139.5", "quotes": {"best_bid": "139", "best_ask": "140"}} for c in COINS15]}
        if "FUNDING:" in url:
            return {"result": []}
        end = int(url.split("end=")[1].split("&")[0])
        xs = rising()
        return {"result": [{"time": end - 86400 * (len(xs) - j), "close": x} for j, x in enumerate(xs)]}
    eng, s = make(tmp_path, BETrendSleeve, fetch)
    s.step(DAY0 + 200)
    assert set(s.state["pos"]) == {"BTC", "ETH"}
    assert sum(p["qty"] * 140 for p in s.state["pos"].values()) <= 0.101 * 1.5 * 10000 * 1.01
    assert set(s.quotes()) == {"BTC", "ETH"}
    s.close_all(DAY0 + 300, "test")
    assert s.state["pos"] == {} and s.state["events"][-1]["kind"] == "RETIRED"
    drain(eng)


def test_dip_buyer_sizes_with_its_blend_leverage(tmp_path):
    from scalper.futures import DipBuyer
    eng, _ = make(tmp_path, TrendSleeve, lambda u: {"result": []})
    d = DipBuyer(eng, 10000.0, fetch=lambda u: {"result": []}, book="be-eth-dip", state_key="be_eth_dip", leverage=0.453 * 1.5)
    d.open(DAY0, 2000.0, DAY0 - 86400, {"close": 1990, "above_pct": 3.0, "last4": [2100, 2050, 2020, 1990]})
    assert d.state["pos"]["qty"] == 3.39 and d.view()["book"] == "be-eth-dip"
    drain(eng)
