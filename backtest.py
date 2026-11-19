"""Backtest the scalping agent's own code on Delta Exchange India's 1-minute history (last ~3 months), with no retuning.

The setups, cost gate, position sizing, risk limits and fee accounting are the live engine's (scalper/engine.py, setups.py),
so this tests exactly what runs live. The rules were written on 2026-11-10 before this history was examined: this is an
out-of-sample test of fixed rules. Nothing is tuned on the result.

Differences from live, all stated on the results page:
- Only 1-minute bars exist for the past. A signal at a bar's close fills at the next bar's open plus half the spread and
  the configured slippage. Exits use each bar's high and low. When a stop and a target are both inside one bar, the stop is
  assumed to come first (the pessimistic order); a target needs the bar to trade strictly through it (no touch-fills).
- The buy/sell split of trades is not in the history, so order flow is approximated by the bar's direction
  (an up bar = buyers in control). Live, the agent uses the real aggressive buy/sell volume.
- The spread is fixed at the live median (BTC 0.07 bp, ETH 0.25 bp); funding is ignored (holds are at most 60 minutes).
Run: python3 backtest.py   (downloads and caches the history in backtest/; writes backtest/results.json)
"""
import json
import math
import os
import random
import shutil
import ssl
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine, utc  # noqa: E402
from scalper.market import bp  # noqa: E402
from scalper.setups import SETUPS, evaluate  # noqa: E402

OUT = ROOT / "backtest"
API = "https://api.india.delta.exchange"
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)
DAYS = 92
SPREAD_BP = {"BTCUSD": 0.07, "ETHUSD": 0.25}
PRODUCTS = {"BTCUSD": {"tick": 0.5, "contract": 0.001, "maker": 0.0002, "taker": 0.0005, "index": ".DEXBTUSD"},
            "ETHUSD": {"tick": 0.05, "contract": 0.01, "maker": 0.0002, "taker": 0.0005, "index": ".DEETHUSD"}}


def get(path):
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "scalper-backtest"}), timeout=30, context=CTX) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 + 3 * k)
    raise RuntimeError(path)


def history(sym, start, end):
    cache = OUT / f"{sym}_1m.json"
    if cache.exists():
        d = json.loads(cache.read_text())
        if d["start"] <= start and d["end"] >= end - 3600:
            return d["bars"]
    rows, hi = {}, end
    while hi > start:
        lo = max(start, hi - 1990 * 60)
        for r in get(f"/v2/history/candles?resolution=1m&symbol={sym}&start={lo}&end={hi}")["result"] or []:
            rows[int(r["time"])] = [float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"] or 0)]
        hi = lo
        time.sleep(0.1)
    bars = [[t] + rows[t] for t in sorted(rows)]
    cache.write_text(json.dumps({"start": start, "end": end, "bars": bars}))
    return bars


def run(halts=True, order="pessimistic"):
    OUT.mkdir(exist_ok=True)
    end = int(time.time()) // 60 * 60 - 60
    start = end - DAYS * 86400
    data = {s: history(s, start - 300 * 60, end) for s in PRODUCTS}
    tmp = OUT / "engine"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    shutil.copy(ROOT / "config.toml", tmp / "config.toml")
    eng = Engine(tmp, PRODUCTS)
    eng.conf = dict(eng.conf, events={"times": []})   # macro events of the past are not listed; no blackout applied
    if not halts:  # diagnostic only: the setups over the whole period, with the loss halts switched off
        eng.L = dict(eng.L, daily_loss_pct=100, weekly_loss_pct=100, max_drawdown_pct=100, consecutive_losses_pause=10**6,
                     consecutive_losses_day=10**6, max_trades_per_day=10**6, half_size_drawdown_pct=100)
    trades, signals, rejected = [], defaultdict(int), defaultdict(int)
    pending = {}
    by_t = {s: {b[0]: b for b in bars} for s, bars in data.items()}
    times = sorted(set().union(*[set(x) for x in by_t.values()]))
    eq_curve, last_eq_t = [], 0
    gross_before_fees = 0.0

    def set_quote(i, px, t):
        half = SPREAD_BP[i.symbol] / 2e4
        i.bid, i.ask = px * (1 - half), px * (1 + half)
        i.bq = i.aq = 1e9
        i.l1_rx = i.index_rx = t
        i.index = px

    for t in times:
        for sym, i in eng.inst.items():
            b = by_t[sym].get(t)
            if not b:
                continue
            _, o, h, lo, c, v = b
            close_t = t + 60
            # 1. a signal from the previous close fills at this bar's open
            if sym in pending:
                sig = pending.pop(sym)
                side = 1 if sig["side"] == "BUY" else -1
                px = o * (1 + side * (SPREAD_BP[sym] / 2e4 + eng.C["slippage_bp"] / 1e4))
                slip = side * bp(px, sig["entry"]["price"])
                if slip > max(eng.L["max_entry_slip_bp"], 0.25 * sig["stop_bp"]):
                    rejected["entry cancelled: price ran away"] += 1
                else:
                    set_quote(i, o, t)
                    o_ = {"id": eng._oid(), "t": t, "time": utc(t), "symbol": sym, "side": side, "qty": sig["size"]["qty"], "kind": "entry",
                          "type": "market", "ready": t, "signal": sig, "status": "pending"}
                    eng.pending[sym] = o_
                    i.ask, i.bid = (px, px * (1 - SPREAD_BP[sym] / 1e4)) if side > 0 else (px * (1 + SPREAD_BP[sym] / 1e4), px)
                    eng.C = dict(eng.C, slippage_bp=0.0)       # already included in px above
                    eng._fill_pending(i, t + 1)
                    eng.C = dict(eng.C, slippage_bp=1.0)
            # 2. exits inside this bar, pessimistic order
            p = eng.pos.get(sym)
            if p:
                s = p["side"]
                adverse, favour = (lo, h) if s > 0 else (h, lo)
                slip = eng.C["slippage_bp"] / 1e4
                done = False
                if s * (o - p["stop"]) <= 0:                       # gapped through the stop at the open
                    eng._exit_fill(p, p["qty"], o * (1 - s * slip), "taker", t, "stop-loss (gap)")
                    eng._close(p, t, "stop-loss")
                    done = True
                elif s * (adverse - p["stop"]) <= 0 and not (order == "optimistic" and s * (favour - p["tp1" if not p["tp1_done"] else "tp2"]) > 0):
                    # stop inside the bar: assumed before any target (pessimistic); the optimistic variant lets a target in the same bar go first
                    why = "stop at entry after TP1" if p["tp1_done"] else "stop-loss"
                    eng._exit_fill(p, p["qty"], p["stop"] * (1 - s * slip), "taker", t, why)
                    eng._close(p, t, why)
                    done = True
                else:
                    if not p["tp1_done"] and s * (favour - p["tp1"]) > 0:
                        half = math.floor(p["qty0"] / 2 / i.contract) * i.contract
                        if half > 0:
                            eng._exit_fill(p, half, p["tp1"], "maker", t, "take-profit 1")
                        p["tp1_done"], p["stop"] = True, p["entry"]
                        if order == "optimistic" and s * (favour - p["tp2"]) > 0:
                            eng._exit_fill(p, p["qty"], p["tp2"], "maker", t, "take-profit 2")
                            eng._close(p, t, "take-profit 2")
                            done = True
                        elif s * (adverse - p["entry"]) <= 0:         # same bar came back to entry: breakeven exit (pessimistic)
                            eng._exit_fill(p, p["qty"], p["entry"] * (1 - s * slip), "taker", t, "stop at entry after TP1")
                            eng._close(p, t, "stop at entry after TP1")
                            done = True
                    if not done and p["tp1_done"] and s * (favour - p["tp2"]) > 0:
                        eng._exit_fill(p, p["qty"], p["tp2"], "maker", t, "take-profit 2")
                        eng._close(p, t, "take-profit 2")
                        done = True
                    if not done and close_t >= p["time_stop_at"]:
                        eng._exit_fill(p, p["qty"], c * (1 - s * (SPREAD_BP[sym] / 2e4 + slip)), "taker", t, "time stop")
                        eng._close(p, t, "time stop")
                        done = True
                if not done:
                    mv = s * bp(c, p["entry"])
                    p["mfe_bp"], p["mae_bp"] = max(p["mfe_bp"], s * bp(favour, p["entry"])), min(p["mae_bp"], s * bp(adverse, p["entry"]))
            # 3. the bar closes: features, setups, gate, risk
            i.bars.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v * i.contract, "buy": v * i.contract if c >= o else 0.0,
                           "sell": 0.0 if c >= o else v * i.contract, "n": 1, "live": True})
            set_quote(i, c, close_t)
            if t < start:
                continue
            f = i.compute()
            cands, _ = evaluate(sym, list(i.bars), f, include_retired=True)   # the backtest replays them all
            for cand in cands:
                sig = eng._signal(i, cand, f, close_t)
                if sig["side"] in ("BUY", "SELL"):
                    signals[cand["setup"]] += 1
                    pending[sym] = sig
                    break
                rejected[sig["why"].split(":")[0].split(" (")[0][:60]] += 1
        eng._roll_day(t + 60)
        if t - last_eq_t >= 3600:
            eq_curve.append([t, round(eng.equity(), 2)])
            last_eq_t = t
        while not eng.out.empty():             # the backtest keeps trades in memory; the database is not used
            kind, x = eng.out.get()
            if kind == "trade":
                trades.append(x)
    eq_curve.append([times[-1], round(eng.equity(), 2)])
    return eng, trades, dict(signals), dict(rejected), eq_curve, start, end


def stats(ts):
    nets = [t["net"] for t in ts]
    bps = [t["net_bp"] for t in ts]
    if not ts:
        return {"trades": 0}
    w, l = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    m = sum(bps) / len(bps)
    sd = math.sqrt(sum((x - m) ** 2 for x in bps) / (len(bps) - 1)) if len(bps) > 1 else 0
    gross = sum(t["gross"] for t in ts)
    return {"trades": len(ts), "net": round(sum(nets), 2), "gross": round(gross, 2), "fees": round(sum(t["fees"] for t in ts), 2),
            "win_rate": round(len(w) / len(ts) * 100, 1), "tp1_rate": round(sum(t["hit_tp1"] for t in ts) / len(ts) * 100, 1),
            "avg_win": round(sum(w) / len(w), 2) if w else None, "avg_loss": round(sum(l) / len(l), 2) if l else None,
            "profit_factor": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None, "avg_net_bp": round(m, 2),
            "avg_gross_bp": round(sum(t["gross"] / (t["qty"] * t["entry"]) * 1e4 for t in ts) / len(ts), 2),
            "t_stat": round(m / sd * math.sqrt(len(bps)), 2) if sd else None,
            "ci95_bp": [round(m - 1.96 * sd / math.sqrt(len(bps)), 2), round(m + 1.96 * sd / math.sqrt(len(bps)), 2)] if sd else None,
            "avg_hold_min": round(sum(t["held_min"] for t in ts) / len(ts), 1),
            "exit_reasons": {k: sum(1 for t in ts if t["exit_reason"] == k) for k in sorted({t["exit_reason"] for t in ts})}}


def summarize(eng, trades, signals, rejected, eq, start, end):
    cap = eng.acct["capital"]
    peak, dd = cap, 0.0
    for _, e in eq:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    by = lambda key: {k: stats([t for t in trades if key(t) == k]) for k in sorted({key(t) for t in trades})}  # noqa: E731
    days = (end - start) / 86400
    overall = stats(trades)
    checks = [("At least 100 trades", overall.get("trades", 0) >= 100, overall.get("trades", 0)),
              ("Average trade above zero after costs, t ≥ 2", (overall.get("t_stat") or 0) >= 2, overall.get("t_stat")),
              ("Profit factor ≥ 1.3", (overall.get("profit_factor") or 0) >= 1.3, overall.get("profit_factor")),
              ("Max drawdown ≤ 6%", dd * 100 <= 6, round(dd * 100, 2))]
    return {"from": utc(start), "to": utc(end), "days": round(days, 1), "capital": cap, "final_equity": round(eng.equity(), 2),
            "return_pct": round((eng.equity() / cap - 1) * 100, 2), "annualized_simple_pct": round((eng.equity() / cap - 1) * 100 * 365 / days, 1),
            "max_dd_pct": round(dd * 100, 2), "halted": eng.acct["halted"], "overall": overall, "by_setup": by(lambda t: t["setup"]),
            "by_symbol": by(lambda t: t["symbol"]), "by_side": by(lambda t: t["side"]), "monthly": by(lambda t: t["exit_time"][:7]),
            "signals": signals, "rejected": rejected, "equity": eq, "trades": trades[::-1], "checks": [{"check": a, "ok": b, "value": c} for a, b, c in checks]}


def main():
    t0 = time.time()
    res = {"generated": utc(time.time()), "setups": {k: v["hypothesis"] for k, v in SETUPS.items()}, "spread_bp": SPREAD_BP, "variants": {}}
    for name, kw in (("as configured", {}), ("full period, halts off", {"halts": False}), ("full period, halts off, optimistic fills", {"halts": False, "order": "optimistic"})):
        eng, trades, signals, rejected, eq, start, end = run(**kw)
        res["variants"][name] = summarize(eng, trades, signals, rejected, eq, start, end)
        res["fees_bp"] = eng.fees
        v = res["variants"][name]
        o = v["overall"]
        print(f"{name}: return {v['return_pct']}% maxDD {v['max_dd_pct']}% halted={bool(v['halted'])} | trades {o.get('trades')} net {o.get('net')} gross {o.get('gross')} "
              f"fees {o.get('fees')} win {o.get('win_rate')}% avg net {o.get('avg_net_bp')} bp gross {o.get('avg_gross_bp')} bp t {o.get('t_stat')} PF {o.get('profit_factor')}", flush=True)
        for k, x in v["by_setup"].items():
            print(f"    {k}: {x['trades']} trades, net {x['net']}, avg gross {x['avg_gross_bp']} bp, avg net {x['avg_net_bp']} bp, t {x['t_stat']}", flush=True)
    res["runtime_s"] = round(time.time() - t0, 1)
    (OUT / "results.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
