"""Candidate setups (skill section 6). Each is a written hypothesis with exact rules, fixed before trading, version 1, in PAPER
only, collecting evidence. None has passed a pre-registered test yet; the dashboard labels them "exploring". Every candidate
must still pass the cost gate and the risk checks in engine.py before any paper order.

Targets and stops are sized for moves of 30-150 bp, never for 1-5 minute scalps: at Delta's retail fees those lose (skill §0)."""

SETUPS = {
    "vol-breakout v1": {
        "hypothesis": "After a volatility squeeze, a break of the 1-hour range on heavy one-sided volume traps traders on the "
                      "wrong side; their stop orders push price further.",
        "min_stop_bp": 25, "stop_atr5": 1.2, "tp_r": (1.5, 3.0), "time_stop_min": 60},
    "sweep-reclaim v1": {
        "hypothesis": "A quick push through the 4-hour high or low that is reclaimed within minutes was a stop hunt; the traders "
                      "who chased it are now trapped and price returns into the range.",
        "min_stop_bp": 20, "stop_atr5": 0.3, "tp_r": (1.5, 2.5), "time_stop_min": 45},
    "flush-reversal v1": {
        "hypothesis": "A one-minute flush of 3x normal range on 4x normal volume is forced liquidation; when the next minute "
                      "reverses, the forced selling (or buying) is done and price snaps back.",
        "min_stop_bp": 25, "stop_atr5": 0.2, "tp_r": (1.5, 3.0), "time_stop_min": 30},
}


# Retired 2026-11-10 by the skill's §9 rule: in the 3-month backtest (backtest.py, 922 trades) every setup lost before fees
# as well as after (t = -14). They take no new trades; new hypotheses must pass a pre-registered test first.
RETIRED = {"vol-breakout v1": "lost 4.5-5.7 bp per trade before fees in the 3-month backtest",
           "sweep-reclaim v1": "lost 6.5 bp per trade before fees in the 3-month backtest",
           "flush-reversal v1": "lost 1.1-1.5 bp per trade before fees in the 3-month backtest"}


def _bp(a, b):
    return (a / b - 1) * 1e4


def evaluate(sym: str, bars: list, f: dict, include_retired: bool = False) -> tuple[list, list]:
    """Return (candidates, near-misses). A candidate has side, entry reference, stop, targets and reasoning; a near-miss is
    a short note of which setup was closest and what was missing, for the no-trade record."""
    out, notes = [], []
    if not f.get("ready"):
        return out, [f"warming up: {f.get('bars', 0)} of 250 one-minute bars"]
    last, prev = bars[-1], bars[-2]
    c, atr5 = last["c"], f["atr5"] or f["atr1"] * 2.2
    atr5_bp = atr5 / c * 1e4

    # 1. volatility-expansion breakout
    s = SETUPS["vol-breakout v1"]
    up, dn = c > f["hi60"] and _bp(c, f["hi60"]) >= 5, c < f["lo60"] and _bp(f["lo60"], c) >= 5
    if up or dn:
        side = 1 if up else -1
        miss = []
        if f["compression"] is None or f["compression"] > 0.7:
            miss.append(f"no squeeze before the break (compression {f['compression']:.2f}, needs <= 0.70)")
        if (f["relvol"] or 0) < 2:
            miss.append(f"volume {f['relvol'] or 0:.1f}x normal, needs 2x")
        if f["delta"] is None or side * f["delta"] < 0.2:
            miss.append(f"order flow not one-sided ({'n/a' if f['delta'] is None else f'{f['delta']:+.2f}'}, needs {'+' if side > 0 else '-'}0.20)")
        if f["vwap"] and side * (c - f["vwap"]) < 0:
            miss.append("price on the wrong side of VWAP")
        if not miss:
            stop_bp = max(s["min_stop_bp"], s["stop_atr5"] * atr5_bp)
            lvl = f["hi60"] if up else f["lo60"]
            out.append({"setup": "vol-breakout v1", "side": side, "ref": c, "stop_bp": stop_bp,
                        "reasoning": f"{sym} broke its 1-hour {'high' if up else 'low'} {lvl:,.2f} after a squeeze (short-term volatility "
                                     f"{f['compression']:.2f}x the 4-hour level), on {f['relvol']:.1f}x normal volume with "
                                     f"{abs(f['delta']) * 100:.0f}% of aggressive volume on the {'buy' if up else 'sell'} side. Traders positioned "
                                     f"for the range are trapped and their stops add fuel. Regime: {f['regime']}.",
                        "invalidation": f"a close back inside the 1-hour range ({'below' if up else 'above'} {lvl:,.2f}) or no follow-through within the time stop"})
        else:
            notes.append(f"breakout {'up' if up else 'down'} but " + "; ".join(miss))

    # 2. sweep and reclaim of the 4-hour extreme
    s = SETUPS["sweep-reclaim v1"]
    recent = bars[-6:-1]
    swept_lo = min(b["l"] for b in recent + [last]) < f["lo240"] and _bp(f["lo240"], min(b["l"] for b in recent + [last])) >= 8
    swept_hi = max(b["h"] for b in recent + [last]) > f["hi240"] and _bp(max(b["h"] for b in recent + [last]), f["hi240"]) >= 8
    for side, swept, lvl in ((1, swept_lo, f["lo240"]), (-1, swept_hi, f["hi240"])):
        if not swept:
            continue
        back = c > lvl if side > 0 else c < lvl
        flow_ok = f["delta"] is not None and side * f["delta"] >= 0.2
        if back and flow_ok:
            ext = min(b["l"] for b in recent + [last]) if side > 0 else max(b["h"] for b in recent + [last])
            stop_bp = max(s["min_stop_bp"], abs(_bp(c, ext)) + s["stop_atr5"] * atr5_bp)
            out.append({"setup": "sweep-reclaim v1", "side": side, "ref": c, "stop_bp": stop_bp,
                        "reasoning": f"{sym} pushed {abs(_bp(ext, lvl)):.0f} bp through its 4-hour {'low' if side > 0 else 'high'} {lvl:,.2f} and "
                                     f"closed back inside, with {'buyers' if side > 0 else 'sellers'} now in control ({f['delta']:+.2f} order-flow imbalance). "
                                     f"The move hunted stops; the traders who {'sold' if side > 0 else 'bought'} the break are trapped.",
                        "invalidation": f"a new {'low' if side > 0 else 'high'} beyond {ext:,.2f}"})
        else:
            notes.append(f"swept the 4-hour {'low' if side > 0 else 'high'} but " + ("not reclaimed yet" if not back else "order flow does not confirm"))

    # 3. liquidation flush, then reversal
    s = SETUPS["flush-reversal v1"]
    prev_rng, prev_ret = prev["h"] - prev["l"], prev["c"] - prev["o"]
    pv = [b["v"] for b in bars[-62:-2]]
    rel_prev = prev["v"] / (sum(pv) / len(pv)) if sum(pv) else 0
    if prev_rng >= 3 * f["atr1"] and rel_prev >= 4:
        side = 1 if prev_ret < 0 else -1          # fade the flush
        retrace = (c - prev["c"]) * side / prev_rng if prev_rng else 0
        if side * (c - last["o"]) > 0 and retrace >= 0.25:
            ext = prev["l"] if side > 0 else prev["h"]
            stop_bp = max(s["min_stop_bp"], abs(_bp(c, ext)) + s["stop_atr5"] * atr5_bp)
            out.append({"setup": "flush-reversal v1", "side": side, "ref": c, "stop_bp": stop_bp,
                        "reasoning": f"{sym} flushed {abs(_bp(prev['c'], prev['o'])):.0f} bp in one minute ({prev_rng / f['atr1']:.1f}x the normal range, "
                                     f"{rel_prev:.1f}x volume), which looks like forced {'selling' if side > 0 else 'buying'}. This minute reversed "
                                     f"{retrace * 100:.0f}% of it: the forced flow is likely exhausted.",
                        "invalidation": f"a break of the flush extreme {ext:,.2f}"})
        else:
            notes.append("one-minute flush detected but no reversal yet")
    kept = [c for c in out if include_retired or c["setup"] not in RETIRED]
    for c in out:
        if c["setup"] in RETIRED and not include_retired:
            notes.insert(0, f"{c['setup']} would fire ({'long' if c['side'] > 0 else 'short'}) but is retired: {RETIRED[c['setup']]}")
    out = kept
    if not out and not notes:
        notes.append(f"no setup: regime {f['regime']}, price inside the 1-hour range {f['lo60']:,.2f}-{f['hi60']:,.2f}, "
                     f"volume {f['relvol'] or 0:.1f}x normal")
    return out, notes
