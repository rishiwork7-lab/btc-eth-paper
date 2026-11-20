import math
import random
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scalper.advisor import FAMILIES, Advisor, Features, Model, context, scan, session_open  # noqa: E402

LIMITS = tomllib.loads((ROOT / "config.toml").read_text())["limits"]
FEES = {"BTCUSD": {"maker": 2.36, "taker": 5.9}, "ETHUSD": {"maker": 2.36, "taker": 5.9}}
T0 = 1_794_000_000 // 86400 * 86400 + 3 * 3600      # a Thursday-ish 03:00 UTC, away from session opens


def walk(n, px=2000.0, seed=1, t0=T0):
    rng, out = random.Random(seed), []
    for i in range(n):
        o = px
        px *= math.exp(rng.gauss(0, 0.0004))
        out.append([t0 + 60 * i, o, max(o, px) * 1.0002, min(o, px) * 0.9998, px, 100.0])
    return out


@pytest.fixture
def adv(tmp_path):
    return Advisor(tmp_path, FEES, LIMITS, fetch=lambda *a: [])


def test_window_levels_exclude_the_current_candle():
    F = Features()
    bars = walk(400)
    for b in bars:
        F.update(b)
    hi = max(b[2] for b in bars[-60:])
    t = bars[-1][0] + 60
    f = F.update([t, bars[-1][4], hi * 1.01, bars[-1][4], hi * 1.009, 500.0])
    assert f["hi60"] == pytest.approx(hi)           # the breakout candle's own high is not in its reference level
    assert f["c"] > f["hi60"]
    assert any(c["family"] == "breakout" and c["side"] == 1 for c in scan("ETHUSD", f))


def test_features_do_not_depend_on_later_candles():
    bars = walk(450, seed=3)
    A, B = Features(), Features()
    fa = [A.update(b) for b in bars[:420]][-1]
    fb = [B.update(b) for b in bars][419]
    assert fa == fb


def _open_idea(adv, sym="ETHUSD", side=1, entry=2000.0, stop_bp=25.0):
    rec = {"id": "A-x", "t": T0, "time_utc": "", "time_ist": "", "source": "live", "symbol": sym, "family": "breakout", "family_name": "Range breakout",
           "side": "BUY" if side > 0 else "SELL", "dir": side, "status": "UNPROVEN", "entry": entry, "stop_bp": stop_bp, "target_r": 2.0,
           "time_min": 90, "regime": "range", "score": 0.4, "exp_gross_r": 0.2, "exp_net_r": 0.1, "spread_bp": 0.25,
           "costs_bp": adv._costs(sym, 0.25), "helps": [], "hurts": [], "z": {k: 0.0 for k in context(sym, _ctx_f(), 1, 25)},
           "features": {"c": entry, "ema240": entry * 0.99}, "risk_usd": 25.0, "outcome": None,
           "path": {"mfe_bp": 0.0, "mae_bp": 0.0, "bars": 0, "relvol_sum": 0.0, "max_range_atr": 0.0, "t_mfe": None, "first_bp": None}}
    adv._levels(rec)
    adv.open[rec["id"]] = rec
    adv.books["live"].d["open"] = 1
    return rec


def _ctx_f():
    return {"tc": T0, "c": 2000.0, "o": 1999.0, "ema240": 1990.0, "mom60_bp": 10.0, "atr5_bp": 20.0, "atr5": 4.0, "vwap": 1995.0,
            "compression": 0.8, "relvol": 1.2, "rv240": 40.0, "hi240": 2010.0, "lo240": 1980.0}


def test_candle_touching_stop_and_target_counts_as_the_stop(adv):
    rec = _open_idea(adv)
    adv._advance(rec, [T0, 2000.0, rec["target"] * 1.001, rec["stop"] * 0.999, 2000.0, 100.0], None)
    assert rec["outcome"] == "stop" and rec["net_r"] < -1


def test_target_needs_a_trade_through_it(adv):
    rec = _open_idea(adv)
    adv._advance(rec, [T0, 2000.0, rec["target"], 1999.0, 2001.0, 100.0], None)
    assert rec["outcome"] is None                    # a touch is not a fill
    adv._advance(rec, [T0 + 60, 2001.0, rec["target"] + 0.05, 2000.0, 2002.0, 100.0], None)
    assert rec["outcome"] == "target"


def test_net_r_is_gross_minus_delta_costs(adv):
    rec = _open_idea(adv)
    adv._advance(rec, [T0, 2000.0, rec["target"] + 1, 1999.0, 2002.0, 100.0], None)
    c = rec["costs_bp"]
    assert rec["gross_r"] == pytest.approx(2.0, abs=1e-6)
    assert rec["net_r"] == pytest.approx((50 - c["entry"] - c["exit_target"]) / 25, abs=1e-3)
    assert c["entry"] == pytest.approx(5.9 + 0.125 + 1.0)
    assert adv.books["live"].d["equity"] == pytest.approx(10000 + 25 * rec["net_r"], abs=0.05)


def test_time_stop_exits_at_the_close_less_costs(adv):
    rec = _open_idea(adv)
    for i in range(90):
        adv._advance(rec, [T0 + 60 * i, 2000.0, 2001.0, 1999.0, 2000.5, 100.0], None)
        if rec["outcome"]:
            break
    assert rec["outcome"] == "time" and rec["held_min"] == 90 and rec["explanation"]["headline"].startswith("No follow-through")


def test_untrained_model_is_exactly_no_edge_before_costs():
    for k, p in FAMILIES.items():
        m = Model(p["target_r"])
        s = m.score({f: 0.0 for f in m.w})
        assert s * (p["target_r"] + 1) - 1 == pytest.approx(0.0, abs=1e-9)


def test_learning_moves_the_score_toward_the_outcome():
    m = Model(2.0)
    z = {f: (1.0 if f == "trend" else 0.0) for f in m.w}
    before = m.score(z)
    m.learn(z, 1.0)
    assert m.score(z) > before and m.w["trend"] > 0
    m2 = Model(2.0)
    m2.learn(z, 0.0)
    assert m2.score(z) < before


def test_first_candidates_are_skipped_not_recommended(adv):
    bars = walk(420, seed=5)
    for b in bars:
        adv.on_bar("ETHUSD", b, "history")
    recs = [x for k, x in list(adv.out.queue) if k == "rec"]
    assert recs and all(r["status"] == "SKIPPED" for r in recs[:3])
    assert "model expects" in recs[0]["status_why"]


def test_restart_keeps_model_and_open_ideas(tmp_path):
    a = Advisor(tmp_path, FEES, LIMITS, fetch=lambda *a: [])
    for b in walk(900, seed=7):
        a.on_bar("ETHUSD", b, "history")
    a.last_t["BTCUSD"] = a.last_t["ETHUSD"]
    import sqlite3
    db = sqlite3.connect(str(a.dbpath))
    db.execute("INSERT OR REPLACE INTO state VALUES ('advisor', ?)", (a._state(),))
    db.commit()
    db.close()
    b = Advisor(tmp_path, FEES, LIMITS, fetch=lambda *a: [])
    assert b.models["breakout"].w == a.models["breakout"].w
    assert set(b.open) == set(a.open) and b.seq == a.seq and b.last_t == a.last_t


def test_session_opens_follow_daylight_saving():
    nov = int(__import__("datetime").datetime(2026, 11, 19, 8, 15, tzinfo=__import__("datetime").timezone.utc).timestamp())
    assert session_open(nov) == "London"                      # 08:00 GMT in winter
    jul = int(__import__("datetime").datetime(2026, 7, 16, 7, 15, tzinfo=__import__("datetime").timezone.utc).timestamp())
    assert session_open(jul) == "London"                      # 08:00 BST = 07:00 UTC
    ny = int(__import__("datetime").datetime(2026, 11, 19, 14, 45, tzinfo=__import__("datetime").timezone.utc).timestamp())
    assert session_open(ny) == "New York"
    sat = int(__import__("datetime").datetime(2026, 11, 21, 8, 15, tzinfo=__import__("datetime").timezone.utc).timestamp())
    assert session_open(sat) is None


def test_yes_no_inputs_are_named_by_what_was_true():
    from scalper.advisor import feat_name
    assert feat_name("weekend", 0.0) == "weekday" and feat_name("weekend", 1.0) == "weekend"
    assert feat_name("trend", -1.0) == "4-hour trend in its favour"


def test_trades_lists_taken_ideas_with_their_box(adv):
    rec = _open_idea(adv)
    adv.recent.append(rec)
    adv._advance(rec, [T0, 2000.0, rec["target"] + 1, 1999.0, 2002.0, 100.0], None)
    tr = adv.trades("ETHUSD")
    assert [t["id"] for t in tr] == ["A-x"]
    t = tr[0]
    assert t["entry"] == 2000.0 and t["stop"] < 2000.0 < t["target"] and t["end"] == t["exit_t"] == T0 + 60 and t["outcome"] == "target"
    assert adv.trades("BTCUSD") == [] and adv.trades("ETHUSD", lo=T0 + 120) == []
    rec2 = dict(rec, id="A-y", status="SKIPPED")
    adv.recent.append(rec2)
    assert [t["id"] for t in adv.trades("ETHUSD")] == ["A-x"]          # skipped candidates are not trades


def test_open_skipped_candidates_are_drawn_as_shadow_only_while_open(adv):
    rec = _open_idea(adv)
    rec["status"] = "SKIPPED"
    adv.recent.append(rec)
    assert adv.trades("ETHUSD") == []                                   # not a trade
    sh = adv.trades("ETHUSD", shadow=True)
    assert [t["id"] for t in sh] == ["A-x"] and sh[0]["shadow"] is True
    adv._advance(rec, [T0, 2000.0, rec["target"] + 1, 1999.0, 2002.0, 100.0], None)
    assert adv.trades("ETHUSD", shadow=True) == []                      # resolved: no longer current


def test_a_stale_live_quote_is_never_used_as_the_entry_price(tmp_path):
    frozen = {"q": (1700.0, 1700.1, 600.0)}                       # last price seen 10 minutes ago (the feed died)
    a = Advisor(tmp_path, FEES, LIMITS, quote=lambda s: frozen["q"], fetch=lambda *x: [])
    bars = walk(420, seed=5)
    for b in bars:
        a.on_bar("ETHUSD", b, "live")
    recs = [x for k, x in list(a.out.queue) if k == "rec"]
    assert recs and all(r["entry"] is None or abs(r["entry"] / r["signal_px"] - 1) < 0.01 for r in recs)
    assert all(abs((r["entry"] or r["signal_px"]) - 1700.1) > 1 for r in recs)     # never the frozen 1700.1
    frozen["q"] = (bars[-1][4] - 0.05, bars[-1][4] + 0.05, 0.3)                  # a fresh quote is used as before
    a.on_bar("ETHUSD", [bars[-1][0] + 60, bars[-1][4], bars[-1][4] * 1.02, bars[-1][4] * 0.999, bars[-1][4] * 1.019, 500.0], "live")
    new = [x for k, x in list(a.out.queue) if k == "rec" and x["t"] == bars[-1][0] + 120]
    assert all(r["entry"] is not None for r in new)
