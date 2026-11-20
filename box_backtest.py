"""Backtest of the owner's consolidation-box breakout (2026-11-20) on Delta Exchange's BTC and ETH perpetuals.

The rule, written before testing (nothing tuned on the result):
- box: the highest high and lowest low of the last 30 closed candles;
- consolidation: the box height is at most half the average 30-candle range of the previous 24 hours (a tight range);
- breakout: a buy-stop at the box top and a sell-stop at the box bottom; the first one traded through opens the trade
  (a candle opening beyond a level fills at the open; a candle breaking both sides is skipped: its order is unknown);
- stop: the other side of the box; target: the same distance the other way (1:1); a candle touching both counts as the stop;
  no result within 4 hours closes at that candle's close;
- one trade at a time per coin; after it closes the scan starts again.
Costs: taker fee + 18% GST on the entry (a stop order) and on a stop or time exit, maker fee + GST on a target (a resting
limit), plus half the spread and 1 bp slippage on every taker fill. Funding is ignored (most trades last minutes).

Context only (never to pick a "best" one): boxes of 20 and 60 candles, entry only on a candle CLOSE outside the box, no
tightness filter, and the skill's cost gate (only boxes whose 1:1 target is at least 3x the worst round-trip cost).

    python3 box_backtest.py           writes backtest/box.json and prints the results"""
import json
import math
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from breakout_backtest import DAYS, GST, SLIP_BP, TAKER, history, resample, stats  # noqa: E402
from scalper.advisor import SPREAD_BP  # noqa: E402

MAKER = 0.0002
MAX_HOLD_MIN = 240
RULE = {"n": 30, "tight": 0.5, "entry": "touch", "cost_gate": False}
VARIANTS = [{"n": 20, "tight": 0.5, "entry": "touch", "cost_gate": False}, {"n": 60, "tight": 0.5, "entry": "touch", "cost_gate": False},
            {"n": 30, "tight": 0.5, "entry": "close", "cost_gate": False}, {"n": 30, "tight": None, "entry": "touch", "cost_gate": False},
            {"n": 30, "tight": 0.5, "entry": "touch", "cost_gate": True}]


def run(sym: str, bars: list, n: int, tight: float | None, entry: str, cost_gate: bool, minutes: int) -> dict:
    taker, maker = TAKER * (1 + GST) * 1e4, MAKER * (1 + GST) * 1e4
    leg = SPREAD_BP[sym] / 2 + SLIP_BP                      # spread + slippage on a taker fill
    worst_rt = 2 * (taker + leg)
    day = 1440 // minutes
    hold = MAX_HOLD_MIN // minutes
    hi_q, lo_q = deque(), deque()                         # monotonic queues over the last n candles (before the current one)
    ranges, rsum = deque(), 0.0                           # n-candle ranges, for the 24-hour average
    trades, skipped_both, gated, i = [], 0, 0, 0
    pos = None
    for k, (t, o, h, l, c, _) in enumerate(bars):
        if pos:                                           # manage the open trade on this candle
            s, e, stop, tgt = pos["side"], pos["entry"], pos["stop"], pos["target"]
            hit_stop = (l <= stop) if s > 0 else (h >= stop)
            hit_tgt = (h > tgt) if s > 0 else (l < tgt)
            out = None                                    # (how it ended, raw exit price, exit fee, spread + slippage applied?)
            if hit_stop:
                out = ("stop", o if ((o <= stop) if s > 0 else (o >= stop)) else stop, taker, True)   # a gap fills at the open
            elif hit_tgt:
                out = ("target", tgt, maker, False)
            elif k - pos["k"] >= hold:
                out = ("time", c, taker, True)
            if out:
                kind, raw, fee, slipped = out
                px = raw * (1 - s * leg / 1e4) if slipped else raw
                risk = abs(pos["trig"] - stop)
                gross = s * (raw / pos["trig"] - 1) * 1e4
                net_bp = s * (px / e - 1) * 1e4 - taker - fee
                trades.append({"t": pos["t"], "side": s, "kind": kind, "risk_bp": round(risk / pos["trig"] * 1e4, 2), "gross_bp": round(gross, 3),
                               "net_bp": round(net_bp, 3), "net_r": round(net_bp / (risk / pos["trig"] * 1e4), 4), "mins": (k - pos["k"] + 1) * minutes})
                pos = None
        elif len(hi_q) and k >= n and len(ranges) >= day:
            top, bot = hi_q[0][1], lo_q[0][1]
            height_bp = (top / bot - 1) * 1e4
            avg_range = rsum / len(ranges)
            ok = tight is None or height_bp <= tight * avg_range
            if ok and cost_gate and height_bp < 3 * worst_rt:
                ok = False
                gated += 1
            if ok:
                if entry == "touch":
                    up, dn = h > top, l < bot
                    if up and dn and not (o > top or o < bot):
                        skipped_both += 1
                    elif up or dn:
                        s = 1 if (o > top or (up and not o < bot)) else -1
                        trig = (o if o > top else top) if s > 0 else (o if o < bot else bot)
                        stop = bot if s > 0 else top
                        pos = {"t": t, "k": k, "side": s, "trig": trig, "stop": stop, "target": trig + (trig - stop),
                               "entry": trig * (1 + s * leg / 1e4)}
                        # the breakout candle itself: after the entry, its low/high may already reach the stop (pessimistic)
                        if (s > 0 and l <= stop) or (s < 0 and h >= stop):
                            px = stop * (1 - s * leg / 1e4)
                            risk_bp = abs(trig - stop) / trig * 1e4
                            net_bp = s * (px / pos["entry"] - 1) * 1e4 - 2 * taker
                            trades.append({"t": t, "side": s, "kind": "stop", "risk_bp": round(risk_bp, 2), "gross_bp": round(-risk_bp, 3),
                                           "net_bp": round(net_bp, 3), "net_r": round(net_bp / risk_bp, 4), "mins": minutes})
                            pos = None
                else:
                    s = 1 if c > top else -1 if c < bot else 0
                    if s:
                        stop = bot if s > 0 else top
                        pos = {"t": t, "k": k, "side": s, "trig": c, "stop": stop, "target": c + (c - stop), "entry": c * (1 + s * leg / 1e4)}
        # update the windows with this candle (used from the next candle on)
        while hi_q and hi_q[-1][1] <= h:
            hi_q.pop()
        hi_q.append((k, h))
        while lo_q and lo_q[-1][1] >= l:
            lo_q.pop()
        lo_q.append((k, l))
        while hi_q[0][0] <= k - n:
            hi_q.popleft()
        while lo_q[0][0] <= k - n:
            lo_q.popleft()
        if k >= n - 1:
            r = (hi_q[0][1] / lo_q[0][1] - 1) * 1e4
            ranges.append(r)
            rsum += r
            if len(ranges) > day:
                rsum -= ranges.popleft()
    return summarize(trades, bars, skipped_both, gated, taker, maker, leg)


def summarize(trades, bars, skipped_both, gated, taker, maker, leg) -> dict:
    days = (bars[-1][0] - bars[0][0]) / 86400
    net_r = [x["net_r"] for x in trades]
    kinds = {k: sum(1 for x in trades if x["kind"] == k) for k in ("target", "stop", "time")}
    risk = sorted(x["risk_bp"] for x in trades)
    eq, peak, dd, curve, half = 10000.0, 10000.0, 0.0, [], None
    every = max(1, len(trades) // 400)
    for j, x in enumerate(trades):                 # a $10,000 account risking 0.25% per trade (the skill's limit)
        eq *= 1 + 0.0025 * x["net_r"]
        peak, dd = max(peak, eq), max(dd, 1 - eq / peak)
        if half is None and eq <= 5000:
            half = j + 1
        if j % every == 0 or j == len(trades) - 1:
            curve.append([x["t"], round(eq, 2)])
    n = len(trades)
    win_net = [x["net_bp"] for x in trades if x["kind"] == "target"]
    loss_net = [x["net_bp"] for x in trades if x["kind"] == "stop"]
    return {"trades": n, "per_day": round(n / days, 1), "skipped_both": skipped_both, "cost_gated": gated, "outcomes": kinds,
            "target_pct": round(kinds["target"] / n * 100, 1) if n else None,
            "risk_bp_median": risk[len(risk) // 2] if risk else None, "gross_r": stats([x["gross_bp"] / x["risk_bp"] for x in trades if x["risk_bp"]]),
            "net_r": stats(net_r), "net_bp": stats([x["net_bp"] for x in trades]),
            "avg_win_bp": round(sum(win_net) / len(win_net), 2) if win_net else None, "avg_loss_bp": round(sum(loss_net) / len(loss_net), 2) if loss_net else None,
            "breakeven_target_pct": (round(-sum(loss_net) / len(loss_net) / (sum(win_net) / len(win_net) - sum(loss_net) / len(loss_net)) * 100, 1)
                                     if win_net and loss_net else None),
            "account": {"end_usd": round(eq, 2), "max_dd_pct": round(dd * 100, 1), "half_after_trades": half, "risk_pct_per_trade": 0.25},
            "first_half": stats(net_r[: n // 2]), "second_half": stats(net_r[n // 2:]),
            "long": stats([x["net_r"] for x in trades if x["side"] > 0]), "short": stats([x["net_r"] for x in trades if x["side"] < 0]),
            "curve": curve}


def main():
    end = int(time.time()) // 60 * 60
    start = end - DAYS * 86400
    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "days": DAYS, "rule": RULE, "max_hold_min": MAX_HOLD_MIN, "results": {}}
    for sym in ("BTCUSD", "ETHUSD"):
        one = [b for b in history(sym, start, end) if start <= b[0] < end]
        for tf, bars in (("1m", one), ("5m", resample(one, 5))):
            m = 1 if tf == "1m" else 5
            res = {"rule": run(sym, bars, RULE["n"], RULE["tight"], RULE["entry"], RULE["cost_gate"], m), "variants": {}}
            for v in VARIANTS:
                r = run(sym, bars, v["n"], v["tight"], v["entry"], v["cost_gate"], m)
                r.pop("curve")
                res["variants"][f"n{v['n']}_{'tight' if v['tight'] else 'any'}_{v['entry']}{'_gated' if v['cost_gate'] else ''}"] = r
            out["results"][f"{sym}_{tf}"] = res
            r = res["rule"]
            print(f"{sym} {tf}: {r['trades']} trades ({r['per_day']}/day), target first {r['target_pct']}% (need {r['breakeven_target_pct']}%), "
                  f"median risk {r['risk_bp_median']} bp, gross {r['gross_r'].get('avg_bp')} R, net {r['net_r'].get('avg_bp')} R (t {r['net_r'].get('t')}), "
                  f"$10k at 0.25% risk -> ${r['account']['end_usd']:,.0f} (max DD {r['account']['max_dd_pct']}%)", flush=True)
    (ROOT / "backtest" / "box.json").write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
