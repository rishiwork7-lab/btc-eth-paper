import math
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine  # noqa: E402
from scalper.options import FridayCondor, next_entry, option_fee, pick_spread, pick_strikes, settle_value  # noqa: E402
from test_engine import PRODUCTS, drain  # noqa: E402

FRI = 1794592800.0      # 2026-11-13 18:00:00 UTC, a Friday


def test_entry_time_is_friday_1800_utc():
    assert next_entry(FRI - 3600) == FRI and next_entry(FRI + 11 * 60) == FRI + 7 * 86400


def test_strikes_and_wings_bracket_the_price():
    k = pick_strikes([1900 + 20 * i for i in range(15)], 2000.0, 0.0254, 0.0508)
    assert k == {"short_call": 2060, "short_put": 1940, "wing_call": 2100, "wing_put": 1900}


def test_trend_spread_sells_only_the_side_away_from_the_trend():
    listed = [1900 + 20 * i for i in range(15)]
    assert pick_spread(listed, 2000.0, 0.0254, 0.0508, up=True) == {"short_put": 1940, "wing_put": 1900}
    assert pick_spread(listed, 2000.0, 0.0254, 0.0508, up=False) == {"short_call": 2060, "wing_call": 2100}


def test_settlement_and_capped_loss():
    legs = [{"strike": 2060, "call": True, "side": "sell"}, {"strike": 1940, "call": False, "side": "sell"},
            {"strike": 2100, "call": True, "side": "buy"}, {"strike": 1900, "call": False, "side": "buy"}]
    assert settle_value(legs, 2000) == 0                      # inside: all worthless, full credit kept
    assert settle_value(legs, 2080) == pytest.approx(20)
    assert settle_value(legs, 2500) == pytest.approx(40)       # far beyond: capped at the wing width
    assert option_fee(10.0, 37.5, 2000, 0.0001, 0.035, 0.18) == pytest.approx(1.18 * min(0.0001 * 2000 * 37.5, 0.035 * 10 * 37.5))


def chain(code, quotes):
    return {"result": [{"symbol": f"{k}-ETH-{s}-{code}", "strike_price": str(s), "mark_price": str(m), "quotes": {"best_bid": str(b), "best_ask": str(a)}}
                       for (k, s), (b, a, m) in quotes.items()]}


@pytest.mark.parametrize("book", ["fri-night", "weekend", "trend-spread", "be-eth-condor", "be-eth-tight"])
def test_a_full_friday_paper_cycle(tmp_path, book):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    eng = Engine(tmp_path, PRODUCTS)
    eng.inst["ETHUSD"].index, eng.inst["ETHUSD"].index_rx = 2000.0, FRI + 58
    code = "141126" if book in ("fri-night", "be-eth-condor", "be-eth-tight") else "151126"
    strikes = [1880 + 20 * i for i in range(13)]
    q = {}
    for s in strikes:
        q[("C", s)] = (max(0.2, 2000 - s + 8) if s < 2000 else max(0.1, 12 - (s - 2000) / 10), 0, 0)
        q[("P", s)] = (max(0.2, s - 2000 + 8) if s > 2000 else max(0.1, 12 - (2000 - s) / 10), 0, 0)
    q = {k: (b, b + 0.4, b + 0.2) for k, (b, _, _) in q.items()}
    calls = {"n": 0}

    def fetch(url):
        if "deribit" in url:
            return {"result": {"data": [[int(FRI - 3600) * 1000, 70, 70, 70, 70.0]]}}
        if "/v2/history/candles" in url and ".DEETHUSD" in url:     # 20-day average 1900 < spot 2000: an uptrend
            d0 = int(FRI // 86400 * 86400)
            return {"result": [{"time": d0 - 86400 * j, "close": 1900.0} for j in range(0, 22)]}
        if "/v2/tickers?" in url:
            return chain(code, q)
        if "/v2/products/" in url:
            calls["n"] += 1
            return {"result": {"taker_commission_rate": "0.0001", "product_specs": {"premium_commission_rate": 0.035}, "settlement_price": "2001.0" if calls["n"] > 1 else None}}
        raise AssertionError(url)
    c = FridayCondor(eng, 10000.0, fetch=fetch, book=book)
    import time as _t
    real = _t.time
    _t.time = lambda: FRI + 60
    try:
        c.step(FRI + 60)
    finally:
        _t.time = real
    p = c.state["pos"]
    lev = {"be-eth-condor": 7.5 * 0.175 * 1.5, "be-eth-tight": 1.0}.get(book, 7.5)
    assert p and p["credit"] > 0 and p["max_loss_pct"] <= 25.03 and p["qty"] <= math.floor(lev * 10000 / 2000 / 0.01) * 0.01 + 1e-9
    if book == "trend-spread":
        assert [(l["side"], l["call"]) for l in p["legs"]] == [("sell", False), ("buy", False)] and p["trend"]["up"]
    shorts = [l for l in p["legs"] if l["side"] == "sell"]
    assert all(l["px"] == q[("C" if l["call"] else "P", int(l["strike"]))][0] for l in shorts)     # sold at the bid, not the mark
    if book == "be-eth-tight":   # tighter strikes: shorts nearer the price than the 0.8 book would pick
        k = {l["name"]: l["strike"] for l in p["legs"]}
        assert k["short_call"] - 2000 <= 40 and 2000 - k["short_put"] <= 40
    c.step(FRI + (19 if book in ("fri-night", "be-eth-condor", "be-eth-tight") else 43) * 3600)   # after the expiry + 2 min: settle at Delta's price
    assert c.state["pos"] is None and c.state["cash"] > 10000 - 1e-6
    drain(eng)
