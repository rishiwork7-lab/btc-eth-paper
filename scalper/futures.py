"""ETH dip-buy in an uptrend: PAPER trading of the futures idea that passed research E5 (edge/research/e5_futures.py, F2).

Rule (unchanged from the research): once a day, just after the 00:00 UTC daily close of Delta's ETHUSD perpetual,
- if flat: buy when the close is above its 200-day average AND is the 3rd lower close in a row;
- if long: sell at the first close above the 5-day average, or after 10 daily closes.
Size: 1x notional (the paper capital's worth of ETH). No leverage: the worst trade in 6.5 years of history was -19.5% (May 2021).
Payer: panicked and forced sellers in an uptrend; the buyer is paid for absorbing them. Evidence: 55 ETH trades 2020-2026, 67%
won, +2.2% average, t 2.8; positive on 13 of 18 other coins. In downtrends (below the 200-day average) it stays out entirely.

Fills are strict: buys at the live best ask, sells at the live best bid, taker fee + 18% GST on both. Funding is charged from
Delta's own funding history over the hold (longs pay positive funding). No order if the order book is over 5 s old."""
import json
import math
import threading
import time

from .engine import ist, utc
from .options import API, get

RULE = {"sma_long": 200, "down_closes": 3, "sma_exit": 5, "max_days": 10, "leverage": 1.0, "symbol": "ETHUSD"}
STATE_KEY = "futures_dip"
BOOK = "eth-dip"


def signal(closes: list[float]) -> dict:
    """closes: completed daily closes, oldest first. Returns the entry test, with the numbers behind it."""
    if len(closes) < RULE["sma_long"] + 1:
        return {"ok": False, "why": f"only {len(closes)} daily closes; {RULE['sma_long'] + 1} needed"}
    c = closes[-1]
    sma = sum(closes[-RULE["sma_long"]:]) / RULE["sma_long"]
    n = RULE["down_closes"]
    streak = all(closes[-1 - j] < closes[-2 - j] for j in range(n))
    return {"ok": c > sma and streak, "close": c, "sma200": round(sma, 2), "above_pct": round((c / sma - 1) * 100, 2), "down_streak": streak,
            "last4": closes[-n - 1:]}


def exit_due(closes_since_entry: list[float], all_closes: list[float]) -> str | None:
    """closes_since_entry: completed closes after the entry close. Exit at the first close above the 5-day average or day 10."""
    if not closes_since_entry:
        return None
    c = all_closes[-1]
    sma5 = sum(all_closes[-RULE["sma_exit"]:]) / RULE["sma_exit"]
    if c > sma5:
        return f"close {c:,.2f} is above the 5-day average {sma5:,.2f} (the bounce came)"
    if len(closes_since_entry) >= RULE["max_days"]:
        return f"{RULE['max_days']} days without a bounce (time stop)"
    return None


class DipBuyer:
    def __init__(self, engine, capital: float = 10000.0, fetch=get, book: str = BOOK, state_key: str = STATE_KEY, leverage: float = RULE["leverage"],
                 name: str = "ETH dip-buy (futures)"):
        self.eng, self.fetch, self.book, self.key, self.lev, self.name = engine, fetch, book, state_key, leverage, name
        self.state = {"cash": capital, "capital": capital, "last_check_day": None, "pos": None, "events": [], "last_signal": None}
        try:
            import sqlite3
            db = sqlite3.connect(str(engine.dbpath))
            r = db.execute("SELECT v FROM state WHERE k=?", (self.key,)).fetchone()
            db.close()
            if r:
                self.state.update(json.loads(r[0]))
        except Exception:
            pass
        self.lock = threading.Lock()

    def _save(self):
        self.eng.out.put(("opt_state", (self.key, json.dumps(self.state, default=str))))

    def _event(self, now, kind, text, extra=None):
        e = {"t": now, "time": utc(now), "time_ist": ist(now), "book": self.book, "kind": kind, "text": text} | (extra or {})
        self.state["events"] = (self.state["events"] + [e])[-200:]
        self.eng.out.put(("opt_event", e))

    def closes(self, day0: int) -> list[tuple[int, float]]:
        rows = self.fetch(f"{API}/v2/history/candles?resolution=1d&symbol={RULE['symbol']}&start={day0 - 260 * 86400}&end={day0}")["result"] or []
        return sorted((int(r["time"]), float(r["close"])) for r in rows if int(r["time"]) < day0)

    def quote(self):
        i = self.eng.inst.get(RULE["symbol"])
        if i and i.bid and i.ask and time.time() - i.l1_rx <= 5:
            return i.bid, i.ask
        return None

    def taker(self) -> float:
        return self.eng.fees[RULE["symbol"]]["taker"] / 1e4

    def funding(self, t0: float, t1: float) -> float:
        """Summed funding rate (fraction) paid by a long between t0 and t1, from Delta's 8-hourly funding history."""
        try:
            rows = self.fetch(f"{API}/v2/history/candles?resolution=1h&symbol=FUNDING:{RULE['symbol']}&start={int(t0)}&end={int(t1)}")["result"] or []
            return sum(float(r["close"]) for r in rows if int(r["time"]) % 28800 == 0 and t0 < int(r["time"]) <= t1) / 100
        except Exception:
            return 0.0

    def loop(self):
        while True:
            try:
                self.step(time.time())
            except Exception as e:
                self._event(time.time(), "ERROR", f"{type(e).__name__}: {e}")
            time.sleep(30)

    def step(self, now: float):
        with self.lock:
            day0 = int(now // 86400 * 86400)
            p = self.state["pos"]
            if p:
                q = self.quote()
                if q:
                    p["mark_pnl"] = round(p["qty"] * (q[0] - p["entry_px"]) - p["fees"], 2)
            if self.state["last_check_day"] == utc(day0)[:10] or now < day0 + 120:
                return
            rows = self.closes(day0)
            if not rows or rows[-1][0] != day0 - 86400:
                return                       # yesterday's candle not published yet; try again next step
            q = self.quote()
            if not q:
                return                       # no fresh order book; try again next step (still the same day)
            self.state["last_check_day"] = utc(day0)[:10]
            closes = [c for _, c in rows]
            if p:
                since = [c for t, c in rows if t >= p["entry_day"] + 86400]
                why = exit_due(since, closes)
                if why:
                    self.close(now, q[0], why)
                else:
                    self._event(now, "HOLD", f"day {len(since)} of at most {RULE['max_days']}: close {closes[-1]:,.2f} is not yet above the 5-day average "
                                f"{sum(closes[-5:]) / 5:,.2f}; holding")
            else:
                s = signal(closes)
                self.state["last_signal"] = s | {"day": utc(day0)[:10]}
                if s["ok"]:
                    self.open(now, q[1], day0 - 86400, s)
                else:
                    why = (f"close {s['close']:,.2f} is {abs(s['above_pct'])}% below its 200-day average {s['sma200']:,.2f}: a downtrend, so the rule stays out"
                           if s.get("above_pct", 0) <= 0 else f"close {s['close']:,.2f} is in an uptrend ({s['above_pct']}% above the 200-day average) but "
                           f"not after 3 lower closes ({', '.join(f'{x:,.0f}' for x in s['last4'])})") if "close" in s else s["why"]
                    self._event(now, "NO_TRADE", why, {"signal": s})
            self._save()

    def open(self, now, ask, entry_day, s):
        qty = round(math.floor(self.lev * self.state["capital"] / ask / 0.01) * 0.01, 2)
        fee = qty * ask * self.taker()
        self.state["cash"] -= fee
        self.state["pos"] = {"entry_t": now, "entry_time": utc(now), "entry_ist": ist(now), "entry_day": entry_day, "entry_px": ask, "qty": round(qty, 2),
                             "fees": round(fee, 4), "signal": s, "mark_pnl": round(-fee, 2)}
        self._event(now, "ENTER", f"bought {qty:.2f} ETH at {ask:,.2f} (best ask): the close {s['close']:,.2f} is {s['above_pct']}% above the 200-day "
                    f"average and is the 3rd lower close in a row ({', '.join(f'{x:,.0f}' for x in s['last4'])}). Sell at the first close above the "
                    f"5-day average, or after {RULE['max_days']} days. Fee {fee:,.2f}.", {"signal": s})

    def close(self, now, bid, why):
        p = self.state["pos"]
        fee = p["qty"] * bid * self.taker()
        fund = self.funding(p["entry_t"], now) * p["qty"] * p["entry_px"]
        gross = p["qty"] * (bid - p["entry_px"])
        net = gross - p["fees"] - fee - fund
        self.state["cash"] += gross - fee - fund
        rec = {"book": self.book, "entry_time": p["entry_time"], "closed": utc(now), "entry_px": p["entry_px"], "exit_px": bid, "qty": p["qty"],
               "gross": round(gross, 2), "fees": round(p["fees"] + fee, 2), "funding": round(fund, 2), "net": round(net, 2),
               "net_pct": round(net / self.state["capital"] * 100, 3), "reason": why, "spot": p["entry_px"], "legs": []}
        self.eng.out.put(("opt_trade", rec))
        self.state["pos"] = None
        self._event(now, "EXIT", f"sold {p['qty']:.2f} ETH at {bid:,.2f} (best bid): {why}. Net {net:+,.2f} ({rec['net_pct']:+.2f}%) after fees "
                    f"{rec['fees']:,.2f} and funding {fund:,.2f}", {"net": net})

    def view(self) -> dict:
        with self.lock:
            s = json.loads(json.dumps(self.state, default=str))
        s["rule"], s["book"], s["book_name"] = RULE | {"leverage": round(self.lev, 3)}, self.book, self.name
        s["test"] = "E5 F2: 55 trades 2020-26, 67% won, t 2.8"
        s["equity"] = s["cash"] + ((s["pos"] or {}).get("mark_pnl") or 0) + ((s["pos"] or {}).get("fees") or 0) if s["pos"] else s["cash"]
        now = time.time()
        s["next_check_ist"] = ist(int(now // 86400 * 86400) + 86400 + 120)
        return s
