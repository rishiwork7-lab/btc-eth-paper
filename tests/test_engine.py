import json
import math
import shutil
import sqlite3
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine  # noqa: E402
from scalper.setups import evaluate  # noqa: E402

PRODUCTS = {"BTCUSD": {"tick": 0.5, "contract": 0.001, "maker": 0.0002, "taker": 0.0005, "index": ".DEXBTUSD"},
            "ETHUSD": {"tick": 0.05, "contract": 0.01, "maker": 0.0002, "taker": 0.0005, "index": ".DEETHUSD"}}
T0 = 1_794_400_000.0


@pytest.fixture
def eng(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    return Engine(tmp_path, PRODUCTS)


def l1(e, t, bid, ask, sym="ETHUSD", q=5000):
    e.on_message({"type": "l1_orderbook", "symbol": sym, "best_bid": bid, "best_ask": ask, "bid_qty": q, "ask_qty": q, "timestamp": int((t - 0.05) * 1e6)}, rx=t)


def trade(e, t, px, sym="ETHUSD", buy=True, size=10):
    e.on_message({"type": "all_trades", "symbol": sym, "price": px, "size": size, "buyer_role": "taker" if buy else "maker", "timestamp": int(t * 1e6)}, rx=t)


def index(e, t, px, s=".DEETHUSD"):
    e.on_message({"type": "v2/spot_price", "s": s, "p": px}, rx=t)


def drain(e):
    db = sqlite3.connect(str(e.dbpath))
    while not e.out.empty():
        e._write(db, *e.out.get())
    return db


def long_position(e, entry=2000.0, stop=1995.0, tp1=2006.0, tp2=2012.0, qty=1.0, t=T0):
    e.pos["ETHUSD"] = {"id": "O-000001", "symbol": "ETHUSD", "side": 1, "qty": qty, "qty0": qty, "entry": entry, "entry_t": t, "entry_time": "x",
                       "stop": stop, "stop0": stop, "tp1": tp1, "tp2": tp2, "tp1_done": False, "time_stop_at": t + 3600, "fees": 0.0, "realized": 0.0,
                       "exits": [], "signal_id": "S-x", "setup": "vol-breakout v1", "confidence": 0.4, "expected_net_bp": 0.0, "status": "exploring",
                       "regime": "squeeze", "mfe_bp": 0.0, "mae_bp": 0.0, "entry_slip_bp": 0.0, "reasoning": "test"}


def test_take_profit_needs_a_trade_through_not_a_touch(eng):
    long_position(eng)
    l1(eng, T0, 2005.95, 2006.0)
    trade(eng, T0 + 1, 2006.0)                      # touches TP1 exactly: our queue place is unknown, no fill
    assert not eng.pos["ETHUSD"]["tp1_done"]
    trade(eng, T0 + 2, 2006.05)                     # prints through TP1: half fills as a maker, stop moves to entry
    p = eng.pos["ETHUSD"]
    assert p["tp1_done"] and p["qty"] == pytest.approx(0.5) and p["stop"] == 2000.0
    assert p["exits"][0]["kind"] == "maker"


def test_stop_fills_at_the_trade_beyond_it_minus_slippage_and_is_recorded(eng):
    long_position(eng)
    l1(eng, T0, 1996.0, 1996.05)
    trade(eng, T0 + 1, 1994.0, buy=False)            # gaps through the 1995 stop
    assert "ETHUSD" not in eng.pos
    db = drain(eng)
    tr = json.loads(db.execute("SELECT data FROM trades").fetchone()[0])
    assert tr["exits"][0]["px"] == pytest.approx(1994.0 * (1 - 1e-4))
    assert tr["net"] < tr["gross"] < 0 and tr["exit_reason"] == "stop-loss" and eng.acct["consec"] == 1


def test_market_entry_waits_the_latency_and_pays_the_taker_fee(eng):
    l1(eng, T0, 2000.0, 2000.05)
    index(eng, T0, 2000.0)
    sig = {"id": "S-1", "side": "BUY", "size": {"qty": 1.0}, "entry": {"price": 2000.05}, "stop_bp": 25, "stop_loss": 1995.0,
           "take_profit": [{"price": 2007.5}, {"price": 2015.0}], "time_stop_min": 60, "setup": "vol-breakout v1", "confidence": 0.4,
           "expected_net_bp": 0, "status": "exploring", "regime": "squeeze", "reasoning": "test"}
    eng._place_entry(eng.inst["ETHUSD"], sig, T0)
    l1(eng, T0 + 0.1, 2000.0, 2000.05)
    assert "ETHUSD" not in eng.pos                    # latency not yet up
    l1(eng, T0 + 0.4, 2000.1, 2000.15)
    p = eng.pos["ETHUSD"]
    assert p["entry"] == pytest.approx(2000.15 * 1.0001)
    assert p["fees"] == pytest.approx(1.0 * p["entry"] * 0.0005 * 1.18)


def test_entry_is_cancelled_if_price_ran_away(eng):
    l1(eng, T0, 2000.0, 2000.05)
    sig = {"id": "S-2", "side": "BUY", "size": {"qty": 1.0}, "entry": {"price": 2000.05}, "stop_bp": 25, "stop_loss": 1995.0,
           "take_profit": [{"price": 2007.5}, {"price": 2015.0}], "time_stop_min": 60, "setup": "vol-breakout v1", "confidence": 0.4,
           "expected_net_bp": 0, "status": "exploring", "regime": "x", "reasoning": "t"}
    eng._place_entry(eng.inst["ETHUSD"], sig, T0)
    l1(eng, T0 + 0.4, 2003.0, 2003.05)               # +15 bp away, over max(3, 0.25 x 25) = 6.25 bp
    assert "ETHUSD" not in eng.pos and "ran" not in sig.get("outcome", "") and "bp from the signal" in sig["outcome"]


def test_cost_gate_rejects_targets_too_small_for_the_fees(eng):
    l1(eng, T0, 2000.0, 2000.05)
    index(eng, T0, 2000.0)
    i = eng.inst["ETHUSD"]
    small = eng._signal(i, {"setup": "vol-breakout v1", "side": 1, "ref": 2000.0, "stop_bp": 5, "reasoning": "r", "invalidation": "i"}, {"regime": "x"}, T0)
    assert small["side"] == "NO_TRADE" and small["why"].startswith("cost gate")
    big = eng._signal(i, {"setup": "vol-breakout v1", "side": 1, "ref": 2000.0, "stop_bp": 30, "reasoning": "r", "invalidation": "i"}, {"regime": "x"}, T0)
    assert big["side"] == "BUY" and big["status"] == "exploring"
    assert big["size"]["risk_pct_equity"] <= 0.125 + 1e-9          # half of 0.25% while exploring
    assert big["expected_gross_bp"] == pytest.approx(0, abs=1e-9)   # no record yet: assumed no edge, never assumed profitable


def test_risk_limits_block_entries(eng):
    i = eng.inst["ETHUSD"]
    l1(eng, T0, 2000.0, 2000.05)
    index(eng, T0, 2000.0)
    assert eng._risk_block(i, T0 + 10).startswith("best bid/ask is")         # stale quote
    l1(eng, T0 + 20, 2000.0, 2001.0)
    index(eng, T0 + 20, 2000.0)
    assert eng._risk_block(i, T0 + 20).startswith("spread")                  # 5 bp spread
    eng.acct["halted"] = "daily loss 1.6% (limit 1.5%) until 00:00 UTC"
    assert eng._risk_block(i, T0 + 20).startswith("daily loss")


def test_restart_continues_ids_and_never_writes_from_the_event_loop(eng, tmp_path):
    before = sqlite3.connect(str(eng.dbpath)).total_changes
    long_position(eng)
    l1(eng, T0, 1996.0, 1996.05)
    trade(eng, T0 + 1, 1994.0, buy=False)
    assert sqlite3.connect(str(eng.dbpath)).total_changes == before and not eng.out.empty()
    eng.seq["order"] = 7
    eng.out.put(("order", {"id": "O-000042", "t": T0}))
    eng.snapshot_state()
    drain(eng)
    again = Engine(tmp_path, PRODUCTS)
    assert again.seq["order"] >= 42 and len(again.stats["vol-breakout v1"]) == 1


def test_breakout_after_a_squeeze_is_detected():
    bars, t, px = [], 1_794_000_000, 2000.0
    for k in range(300):                              # a quiet range, wider swings early (so the last minutes are a squeeze)
        amp = 6.0 if k < 240 else 0.6
        c = px + amp * math.sin(k / 3)
        bars.append({"t": t + 60 * k, "o": c, "h": c + amp / 2, "l": c - amp / 2, "c": c, "v": 10.0, "buy": 5.0, "sell": 5.0, "n": 5, "live": True})
    bars.append({"t": t + 60 * 300, "o": 2001.0, "h": 2012.0, "l": 2000.5, "c": 2011.0, "v": 40.0, "buy": 32.0, "sell": 8.0, "n": 30, "live": True})
    from scalper.market import Instrument
    i = Instrument("ETHUSD", ".DEETHUSD", 0.01, 0.05)
    i.bars.extend(bars)
    f = i.compute()
    cands, notes = evaluate("ETHUSD", bars, f, include_retired=True)
    assert any(c["setup"] == "vol-breakout v1" and c["side"] == 1 for c in cands), notes
    live, live_notes = evaluate("ETHUSD", bars, f)                    # retired after the backtest: never traded live
    assert not live and "retired" in live_notes[0]


def test_scalping_switched_off_keeps_prices_but_never_decides(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    eng = Engine(tmp_path, PRODUCTS)
    eng.scalping = False
    called = []
    eng._on_bar = lambda *a: called.append("bar")
    eng._fill_pending = lambda *a: called.append("fill")
    eng._exits = lambda *a: called.append("exit")
    eng.on_message({"type": "v2/spot_price", "s": eng.inst["ETHUSD"].index_sym, "p": "2000.5"}, rx=1794528000.0)
    eng.on_message({"type": "l1_orderbook", "symbol": "ETHUSD", "best_bid": "2000.0", "best_ask": "2000.05", "timestamp": 1794528000000000}, rx=1794528000.0)
    assert eng.inst["ETHUSD"].index == 2000.5 and eng.inst["ETHUSD"].bid == 2000.0 and called == []
