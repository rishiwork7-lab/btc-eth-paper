"""Backtest of the owner's breakout scalp (2026-11-20) on Delta Exchange's BTC and ETH perpetuals, 1-minute and 5-minute candles.

The rule, exactly as written before testing (nothing tuned on the result):
- a buy-stop sits at the previous candle's high and a sell-stop at its low (placed before the candle opens: no look-ahead);
- if the candle trades above the previous high, go long there; below the previous low, go short there; if it opens beyond
  a level, the fill is the open (a gap);
- "book on the next candle": exit at the close of the next candle;
- one trade at a time; a candle that breaks BOTH levels is skipped, because the candle cannot say which came first.
Costs on every trade: Delta's taker fee plus 18% GST on entry and exit (a stop order and a market exit both take liquidity),
half the spread and 1 bp of slippage each way. Funding is ignored (holds are 1-2 candles).

Variants (shown only for context, never to pick a "best" one): the exit at the close of the breakout candle itself, and the
"recent" high/low taken over the last 5 or 20 candles instead of 1.

    python3 breakout_backtest.py        writes backtest/breakout.json (the page /breakout reads it)
Data: Delta's 1-minute candles (cached in advisor/<SYM>_1m.json by the advisor), topped up to now; 5-minute candles are built
from them, aligned to 5-minute boundaries."""
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scalper.advisor import SPREAD_BP, candles  # noqa: E402

DAYS = 180
TAKER = 0.0005                 # Delta India perpetuals, per side, before GST (product API, 2026-11)
GST = 0.18
SLIP_BP = 1.0
TICK = {"BTCUSD": 0.5, "ETHUSD": 0.05}
RULE = {"lookback": 1, "exit": "next"}
VARIANTS = [{"lookback": 1, "exit": "next"}, {"lookback": 1, "exit": "same"}, {"lookback": 5, "exit": "next"}, {"lookback": 20, "exit": "next"}]


def history(sym: str, start: int, end: int) -> list:
    cache = ROOT / "advisor" / f"{sym}_1m.json"
    bars = json.loads(cache.read_text()) if cache.exists() else []
    bars = [b for b in bars if b[0] >= start]
    if not bars or bars[0][0] > start + 3600:
        bars = candles(sym, start, bars[0][0] if bars else end) + bars
    bars += candles(sym, bars[-1][0] + 60, end)
    return bars


def resample(bars: list, minutes: int) -> list:
    out, cur = [], None
    for t, o, h, l, c, v in bars:
        k = t // (minutes * 60) * minutes * 60
        if cur and cur[0] == k:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], h), min(cur[3], l), c, cur[5] + v
        else:
            if cur:
                out.append(cur)
            cur = [k, o, h, l, c, v]
    if cur and cur[0] + minutes * 60 <= bars[-1][0] + 60:      # only complete candles
        out.append(cur)
    return out


def run(sym: str, bars: list, lookback: int, exit_rule: str) -> dict:
    fee_bp = TAKER * (1 + GST) * 1e4
    cost_side = SPREAD_BP[sym] / 2 + SLIP_BP
    tick = TICK[sym]
    trades, skipped_both, i = [], 0, lookback
    while i < len(bars) - 1:
        t, o, h, l, c, _ = bars[i]
        hi = max(b[2] for b in bars[i - lookback:i])
        lo = min(b[3] for b in bars[i - lookback:i])
        up, dn = h > hi, l < lo
        if up and dn and not (o > hi or o < lo):
            skipped_both += 1
            i += 1
            continue
        if up or dn:
            side = 1 if (o > hi or (up and not o < lo)) else -1
            trig = (o if o > hi else hi + tick) if side > 0 else (o if o < lo else lo - tick)
            j = i + 1 if exit_rule == "next" else i
            ex = bars[j][4]
            gross = side * (ex / trig - 1) * 1e4
            entry_px = trig * (1 + side * cost_side / 1e4)
            exit_px = ex * (1 - side * cost_side / 1e4)
            net = side * (exit_px / entry_px - 1) * 1e4 - 2 * fee_bp
            trades.append((t, side, round(gross, 3), round(net, 3)))
            i = j + 1
            continue
        i += 1
    return summarize(trades, bars, skipped_both, fee_bp, cost_side)


def stats(xs: list) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    win, loss = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
    return {"n": n, "avg_bp": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None, "win_pct": round(sum(1 for x in xs if x > 0) / n * 100, 1),
            "pf": round(win / loss, 3) if loss else None, "sum_pct": round(sum(xs) / 100, 2)}


def account(net: list, per_day: float) -> dict:
    """A real $10,000 account trading 1x its equity on every signal, compounding: when would half of it be gone?"""
    eq, half_at = 10000.0, None
    for k, x in enumerate(net):
        eq *= 1 + x / 1e4
        if half_at is None and eq <= 5000:
            half_at = k + 1
    return {"end_usd": round(eq, 2), "half_after_trades": half_at, "half_after_days": round(half_at / per_day, 2) if half_at else None}


def summarize(trades: list, bars: list, skipped_both: int, fee_bp: float, cost_side: float) -> dict:
    gross, net = [x[2] for x in trades], [x[3] for x in trades]
    days = (bars[-1][0] - bars[0][0]) / 86400
    eq, peak, dd, curve = 0.0, 0.0, 0.0, []
    geq = 0.0
    every = max(1, len(trades) // 400)
    for k, (t, _, g, nt) in enumerate(trades):
        eq += nt / 100
        geq += g / 100
        peak, dd = max(peak, eq), max(dd, peak - eq)
        if k % every == 0 or k == len(trades) - 1:
            curve.append([t, round(geq, 3), round(eq, 3)])
    months: dict = {}
    for t, _, g, nt in trades:
        m = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")
        r = months.setdefault(m, [0, 0.0, 0.0])
        r[0] += 1
        r[1] += g / 100
        r[2] += nt / 100
    half = len(trades) // 2
    g = stats(gross)
    return {"trades": len(trades), "per_day": round(len(trades) / days, 1), "skipped_both": skipped_both, "gross": g, "net": stats(net),
            "long": stats([x[3] for x in trades if x[1] > 0]), "short": stats([x[3] for x in trades if x[1] < 0]),
            "first_half": stats(net[:half]), "second_half": stats(net[half:]), "max_dd_pct": round(dd, 2),
            "costs_bp": {"fees": round(2 * fee_bp, 2), "spread_and_slippage": round(2 * cost_side, 2), "total": round(2 * fee_bp + 2 * cost_side, 2)},
            "breakeven_fee_bp_per_side": round((g.get("avg_bp", 0) - 2 * cost_side) / 2, 2) if g["n"] else None,
            "account": account(net, len(trades) / days),
            "curve": curve, "months": [[m, r[0], round(r[1], 2), round(r[2], 2)] for m, r in sorted(months.items())]}


def main():
    end = int(time.time()) // 60 * 60
    start = end - DAYS * 86400
    out = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "days": DAYS, "rule": RULE, "variants": VARIANTS,
           "costs": {"taker_fee_pct": TAKER * 100, "gst_pct": GST * 100, "slippage_bp": SLIP_BP, "half_spread_bp": {s: v / 2 for s, v in SPREAD_BP.items()}},
           "results": {}}
    for sym in ("BTCUSD", "ETHUSD"):
        one = history(sym, start, end)
        one = [b for b in one if start <= b[0] < end]
        series = {"1m": one, "5m": resample(one, 5)}
        out.setdefault("period", {"from": datetime.fromtimestamp(one[0][0], timezone.utc).strftime("%Y-%m-%d"),
                                  "to": datetime.fromtimestamp(one[-1][0], timezone.utc).strftime("%Y-%m-%d")})
        for tf, bars in series.items():
            res = {}
            for v in VARIANTS:
                key = f"lb{v['lookback']}_{v['exit']}"
                r = run(sym, bars, v["lookback"], v["exit"])
                if v != RULE:
                    r.pop("curve")
                    r.pop("months")
                res[key] = r
            out["results"][f"{sym}_{tf}"] = {"symbol": sym, "tf": tf, "candles": len(bars), "rule": res.pop(f"lb{RULE['lookback']}_{RULE['exit']}"), "variants": res}
            r = out["results"][f"{sym}_{tf}"]["rule"]
            print(f"{sym} {tf}: {r['trades']} trades ({r['per_day']}/day), gross {r['gross']['avg_bp']:+.2f} bp (t {r['gross']['t']}), "
                  f"net {r['net']['avg_bp']:+.2f} bp, win {r['net']['win_pct']}%, total net {r['net']['sum_pct']:+.1f}%", flush=True)
    (ROOT / "backtest" / "breakout.json").write_text(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
