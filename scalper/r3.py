"""R3 blend, PAPER: trend + carry sleeves on Delta India perpetuals (the BTC condor sleeve lives in options.py).

The owner chose R3 on 2026-11-11 after research E6-E11 (edge/research/e12_r3_paper.py: ~26%/yr, Sharpe 1.26, worst drawdown
27.5% over 2021-26 at this exact configuration; every year positive). One shared $10,000 paper account, fixed weights x 1.5:
- Trend sleeve, checked daily after the 00:00 UTC close: each of the 15 coins is held long while its 28-day return is above 0.
  Weights are inverse 30-day volatility over all 15 coins, so the sleeve's gross is at most 0.21 x 1.5 = 0.315x capital.
- Carry sleeve, rebalanced each Monday after 00:00 UTC: rank the 15 coins by their average Binance funding rate over the last
  7 days (the market-wide crowding signal; Delta's own alt funding barely moves). Short the 5 highest, long the 5 lowest,
  inverse-vol weights within each side, 0.35 x 1.5 / 2 = 0.2625x capital per side.
Fills are strict: buys at the live best ask, sells at the live best bid, Delta taker fee + 18% GST. Delta's own funding is
charged on every position at each 8-hour mark. To avoid paying the spread for tiny drifts, a coin is only traded when its
target changes by at least 10% of its target size or $25 (the research rebalanced fully; this trades a little less)."""
import json
import math
import threading
import time
from datetime import datetime, timezone

from .engine import ist, utc
from .options import API, get

COINS15 = "BTC ETH BNB XRP ADA DOGE LTC LINK BCH TRX XLM ETC DASH ZEC XMR".split()
WEIGHTS = {"trend": 0.21, "carry": 0.35, "condor": 0.45}
LEVERAGE = 1.5
BINANCE = "https://fapi.binance.com/fapi/v1"
DAY = 86400


def trend_targets(closes: dict, capital: float, budget_w: float | None = None) -> tuple[dict, dict]:
    """closes: {coin: completed daily closes, oldest first}. Returns ({coin: target notional}, {coin: details})."""
    info = {}
    for c, xs in closes.items():
        if len(xs) < 31:
            continue
        rets = [math.log(xs[-30 + j] / xs[-31 + j]) for j in range(30)]
        m = sum(rets) / 30
        vol = math.sqrt(sum((r - m) ** 2 for r in rets) / 29) * math.sqrt(365)
        if vol > 0:
            info[c] = {"ret28_pct": round((xs[-1] / xs[-29] - 1) * 100, 2), "vol_pct": round(vol * 100, 1), "on": xs[-1] / xs[-29] > 1}
    inv = sum(100 / i["vol_pct"] for i in info.values())
    budget = (WEIGHTS["trend"] * LEVERAGE if budget_w is None else budget_w) * capital
    tgt = {c: (budget * (100 / i["vol_pct"]) / inv if i["on"] else 0.0) for c, i in info.items()}
    return tgt, info


def carry_targets(funding: dict, vols: dict, capital: float) -> tuple[dict, dict]:
    """funding: {coin: avg Binance funding over 7 days}; vols: {coin: annualized vol}. Short top 5, long bottom 5."""
    live = sorted((f, c) for c, f in funding.items() if vols.get(c))
    if len(live) < 10:
        return {}, {"why": f"only {len(live)} coins with funding and volatility data"}
    longs, shorts = [c for _, c in live[:5]], [c for _, c in live[-5:]]
    side = WEIGHTS["carry"] * LEVERAGE * capital / 2
    tgt = {}
    for group, sgn in ((longs, 1), (shorts, -1)):
        inv = sum(1 / vols[c] for c in group)
        for c in group:
            tgt[c] = sgn * side * (1 / vols[c]) / inv
    return tgt, {"longs": longs, "shorts": shorts, "funding_bp_8h": {c: round(f * 1e4, 2) for f, c in live}}


class PerpSleeve:
    key = book = name = ""
    coins = COINS15

    def __init__(self, engine, capital: float = 10000.0, fetch=get):
        self.eng, self.fetch, self.capital = engine, fetch, capital
        self.state = {"realized": 0.0, "fees": 0.0, "funding": 0.0, "unreal": 0.0, "pos": {}, "events": [], "last_run": None, "funded_to": None,
                      "marked_t": 0, "decision": None}
        try:
            import sqlite3
            db = sqlite3.connect(str(engine.dbpath))
            r = db.execute("SELECT v FROM state WHERE k=?", (self.key,)).fetchone()
            db.close()
            if r:
                self.state.update(json.loads(r[0]))
        except Exception:
            pass
        self.specs: dict = {}
        self.lock = threading.Lock()

    # ---- plumbing ---------------------------------------------------------------------------------------------------------
    def _save(self):
        self.eng.out.put(("opt_state", (self.key, json.dumps(self.state, default=str))))

    def _event(self, now, kind, text, extra=None):
        e = {"t": now, "time": utc(now), "time_ist": ist(now), "book": self.book, "kind": kind, "text": text} | (extra or {})
        self.state["events"] = (self.state["events"] + [e])[-200:]
        self.eng.out.put(("opt_event", e))

    def spec(self, coin):
        if coin not in self.specs:
            p = self.fetch(f"{API}/v2/products/{coin}USD")["result"]
            self.specs[coin] = {"cv": float(p["contract_value"]), "taker": float(p["taker_commission_rate"]) * (1 + self.eng.C["gst"])}
        return self.specs[coin]

    def quotes(self) -> dict:
        rows = self.fetch(f"{API}/v2/tickers?contract_types=perpetual_futures")["result"]
        out = {}
        for r in rows:
            c = r["symbol"][:-3] if r["symbol"].endswith("USD") else None
            q = r.get("quotes") or {}
            if c in self.coins and q.get("best_bid") and q.get("best_ask"):
                out[c] = {"bid": float(q["best_bid"]), "ask": float(q["best_ask"]), "mark": float(r.get("mark_price") or 0)}
        return out

    def closes(self, coin, day0) -> list[float]:
        rows = self.fetch(f"{API}/v2/history/candles?resolution=1d&symbol={coin}USD&start={day0 - 45 * DAY}&end={day0}")["result"] or []
        rows = sorted((int(r["time"]), float(r["close"])) for r in rows if int(r["time"]) < day0)
        if not rows or rows[-1][0] != day0 - DAY:
            return []
        return [c for _, c in rows]

    # ---- accounting -------------------------------------------------------------------------------------------------------
    def fill(self, now, coin, dq, q):
        px = q["ask"] if dq > 0 else q["bid"]
        p = self.state["pos"].get(coin, {"qty": 0.0, "avg": 0.0})
        qty, avg = p["qty"], p["avg"]
        realized = 0.0
        if qty == 0 or (qty > 0) == (dq > 0):
            avg = (qty * avg + dq * px) / (qty + dq)
            qty += dq
        else:
            close = min(abs(dq), abs(qty))
            realized = close * (px - avg) * (1 if qty > 0 else -1)
            qty += dq
            if abs(qty) < 1e-12:
                qty, avg = 0.0, 0.0
            elif (qty > 0) == (dq > 0):      # flipped through zero: the rest opens at px
                avg = px
        fee = abs(dq) * px * self.spec(coin)["taker"]
        self.state["realized"] += realized
        self.state["fees"] += fee
        if qty == 0:
            self.state["pos"].pop(coin, None)
        else:
            self.state["pos"][coin] = {"qty": round(qty, 8), "avg": avg}
        rec = {"book": self.book, "entry_time": utc(now), "time_ist": ist(now), "coin": coin, "side": "BUY" if dq > 0 else "SELL", "qty": round(abs(dq), 8),
               "px": px, "notional": round(abs(dq) * px, 2), "fee": round(fee, 4), "realized": round(realized, 2), "net": round(realized - fee, 2)}
        self.eng.out.put(("opt_trade", rec))
        return rec

    def rebalance(self, now, targets: dict, q: dict) -> list:
        done, skipped = [], []
        for c in sorted(set(targets) | set(self.state["pos"])):
            want = targets.get(c, 0.0)
            if c not in q:
                skipped.append(c)
                continue
            mid = (q[c]["bid"] + q[c]["ask"]) / 2
            cv = self.spec(c)["cv"]
            tgt_qty = round(want / mid / cv) * cv
            cur = self.state["pos"].get(c, {}).get("qty", 0.0)
            dq = round(tgt_qty - cur, 8)
            if abs(dq) < cv / 2:
                continue
            if tgt_qty != 0 and cur != 0 and (tgt_qty > 0) == (cur > 0) and abs(dq * mid) < max(25.0, 0.10 * abs(want)):
                continue
            done.append(self.fill(now, c, dq, q[c]))
        if skipped:
            self._event(now, "WARN", f"no live quote for {', '.join(skipped)}: left unchanged this time")
        return done

    def accrue_funding(self, now):
        """Delta's own funding at each 8-hour mark since the last accrual (longs pay a positive rate)."""
        since = self.state["funded_to"] or now
        if self.state["pos"] and now - since >= 3600:
            q = None
            total = 0.0
            for c, p in self.state["pos"].items():
                rows = self.fetch(f"{API}/v2/history/candles?resolution=1h&symbol=FUNDING:{c}USD&start={int(since)}&end={int(now)}")["result"] or []
                rate = sum(float(r["close"]) for r in rows if int(r["time"]) % 28800 == 0 and since < int(r["time"]) <= now) / 100
                if rate:
                    q = q or self.quotes()
                    px = (q.get(c) or {}).get("mark") or p["avg"]
                    total += rate * p["qty"] * px
            self.state["funding"] += total
        self.state["funded_to"] = now

    def mark(self, now):
        q = self.quotes()
        u = 0.0
        for c, p in self.state["pos"].items():
            if c in q:
                exit_px = q[c]["bid"] if p["qty"] > 0 else q[c]["ask"]
                u += p["qty"] * (exit_px - p["avg"])
        self.state["unreal"], self.state["marked_t"] = u, now
        self.state["marks"] = {c: round((v["bid"] + v["ask"]) / 2, 8) for c, v in q.items()}
        return q

    def close_all(self, now: float, why: str):
        """Retire the sleeve: accrue funding, then close every position at the live bid/ask (once)."""
        with self.lock:
            if not self.state["pos"]:
                return
            q = self.mark(now)
            self.accrue_funding(now)
            fills = self.rebalance(now, {}, q)
            self.mark(now)
            self._event(now, "RETIRED", f"{why}. Closed {len(fills)} positions: " + "; ".join(f"{f['side']} {f['qty']:g} {f['coin']} at {f['px']:g}" for f in fills)
                        + f". Final sleeve P&L {self.pnl():+,.2f}.")
            self._save()

    def pnl(self) -> float:
        s = self.state
        return s["realized"] + s["unreal"] - s["fees"] - s["funding"]

    # ---- loop -------------------------------------------------------------------------------------------------------------
    def loop(self):
        while True:
            try:
                self.step(time.time())
            except Exception as e:
                self._event(time.time(), "ERROR", f"{type(e).__name__}: {e}")
            time.sleep(30)

    def due(self, now) -> str | None:
        raise NotImplementedError

    def decide(self, now, day0, q) -> None:
        raise NotImplementedError

    def step(self, now: float):
        with self.lock:
            day0 = int(now // DAY * DAY)
            if now - self.state["marked_t"] >= 60:
                q = self.mark(now)
                self._save()
            else:
                q = None
            run_id = self.due(now)
            if not run_id or self.state["last_run"] == run_id:
                return
            q = q or self.mark(now)
            self.accrue_funding(now)
            self.decide(now, day0, q)
            self.state["last_run"] = run_id
            self.mark(now)
            self._save()

    def view(self) -> dict:
        with self.lock:
            s = json.loads(json.dumps(self.state, default=str))
        s.update(book=self.book, book_name=self.name, capital=self.capital, pnl=round(self.pnl(), 2), weights=WEIGHTS, leverage=LEVERAGE)
        return s


class TrendSleeve(PerpSleeve):
    key, book, name = "r3_trend", "r3-trend", "R3 · Trend (15 coins)"

    def due(self, now):
        day0 = int(now // DAY * DAY)
        return utc(day0)[:10] if now >= day0 + 180 else None

    budget_w = None

    def decide(self, now, day0, q):
        closes = {c: self.closes(c, day0) for c in self.coins}
        missing = [c for c, x in closes.items() if len(x) < 31]
        tgt, info = trend_targets({c: x for c, x in closes.items() if len(x) >= 31}, self.capital, self.budget_w)
        fills = self.rebalance(now, tgt, q)
        on = sorted((c for c, i in info.items() if i["on"]), key=lambda c: -info[c]["ret28_pct"])
        self.state["decision"] = {"day": utc(day0)[:10], "info": info, "targets": {c: round(v, 2) for c, v in tgt.items() if v}}
        gross = sum(tgt.values())
        self._event(now, "REBALANCE" if fills else "HOLD",
                    (f"{len(on)} of {len(info)} coins in an uptrend (28-day return above 0): {', '.join(on) or 'none'}. Target gross ${gross:,.0f} "
                     f"({gross / self.capital:.2f}x). " + (f"{len(fills)} trades: " + "; ".join(f"{f['side']} {f['qty']:g} {f['coin']} at {f['px']:g}" for f in fills)
                                                            if fills else "No trade needed."))
                    + (f" Missing daily data: {', '.join(missing)}." if missing else ""))


class CarrySleeve(PerpSleeve):
    key, book, name = "r3_carry", "r3-carry", "R3 · Funding carry (15 coins)"

    def due(self, now):
        day0 = int(now // DAY * DAY)
        if now < day0 + 300:
            return None
        monday = day0 - datetime.fromtimestamp(day0, timezone.utc).weekday() * DAY
        if not self.state["pos"] and self.state["last_run"] is None:
            return utc(monday)[:10]          # first start: take positions at once (this week's Monday ranking)
        return utc(monday)[:10] if day0 == monday else None

    def binance_funding(self, coin, day0):
        monday = day0 - datetime.fromtimestamp(day0, timezone.utc).weekday() * DAY
        rows = self.fetch(f"{BINANCE}/fundingRate?symbol={coin}USDT&startTime={(monday - 7 * DAY) * 1000}&endTime={monday * 1000}&limit=100")
        xs = [float(r["fundingRate"]) for r in rows] if isinstance(rows, list) else []
        return sum(xs) / len(xs) if xs else None

    def decide(self, now, day0, q):
        monday = day0 - datetime.fromtimestamp(day0, timezone.utc).weekday() * DAY
        vols, funding = {}, {}
        for c in COINS15:
            xs = self.closes(c, monday) if monday == day0 else self.closes(c, day0)
            if len(xs) >= 31:
                rets = [math.log(xs[-30 + j] / xs[-31 + j]) for j in range(30)]
                m = sum(rets) / 30
                vols[c] = math.sqrt(sum((r - m) ** 2 for r in rets) / 29) * math.sqrt(365)
            f = self.binance_funding(c, day0)
            if f is not None:
                funding[c] = f
        tgt, info = carry_targets(funding, vols, self.capital)
        if not tgt:
            return self._event(now, "NO_TRADE", info["why"])
        fills = self.rebalance(now, tgt, q)
        self.state["decision"] = {"week_of": utc(monday)[:10], **info, "targets": {c: round(v, 2) for c, v in tgt.items()}}
        fb = info["funding_bp_8h"]
        self._event(now, "REBALANCE" if fills else "HOLD",
                    f"Week of {utc(monday)[:10]}: crowded longs pay the most funding on {', '.join(f'{c} ({fb[c]:+.2f} bp/8h)' for c in info['shorts'])}: short these. "
                    f"Least crowded: {', '.join(f'{c} ({fb[c]:+.2f})' for c in info['longs'])}: long these. "
                    + (f"{len(fills)} trades: " + "; ".join(f"{f['side']} {f['qty']:g} {f['coin']} at {f['px']:g}" for f in fills) if fills else "No trade needed."))


# ---- BE blend (owner, 2026-11-11: BTC and ETH only). Weights from edge/research/e13_be_paper.py, x 1.5 ----------------------
# Owner, later on 2026-11-11: keep only the parts that perform well -> BTC condor + ETH dip-buy, x 2.0 (e13_be_paper.py).
# Trend (Sharpe 0.54) and the ETH condor (0.58) were removed; BETrendSleeve stays for its records only.
BE_WEIGHTS = {"btc_condor": 0.377, "eth_dip": 0.623}
BE_LEVERAGE = 2.0


class BETrendSleeve(TrendSleeve):
    key, book, name = "be_trend", "be-trend", "Trend (BTC + ETH)"
    coins = ["BTC", "ETH"]
    budget_w = 0.101 * 1.5            # its size while it ran (removed 2026-11-11)
