"""ETH weekend option books, PAPER: the Friday-night condor (edge/research E2/E3) and the weekend trend spread (E5 O3).

Trend spread: the same Friday 18:00 UTC entry, Sunday 12:00 UTC expiry; if the ETH index is above its 20-day average, only the
put side (sell a put, buy the further put); otherwise only the call side. Same strike distances, size and 25% loss cap.

Friday-night condor rule (unchanged from the research):
- every Friday 18:00 UTC (23:30 IST), sell one ETH call and one ETH put expiring Saturday 12:00 UTC (17:30 IST), strikes at
  spot x (1 +/- 0.8 x ETH DVOL x sqrt(18 h / 8760));
- buy the call and put further out at 1.6 x the same distance (the wings), which caps the worst possible loss before entry;
- hold all four to expiry: no stop is needed, the wings are the stop.
Size: 7.5x notional per leg on the paper capital (the owner's choice, 2026-11-10). Backtest: ~47% a year, worst possible night
capped at ~21.5% of capital; that is not a promise (58 Fridays, t = 2.4).

Fills are strict: short legs sell at the live best bid, wings buy at the live best ask (no mid-price fills). Fees are Delta's
option fees from the product API (min of rate x notional and 3.5% of premium) plus 18% GST, on every leg, and again on any leg
settled in the money. Settlement uses Delta's published settlement price, or the 30-minute index average if not yet published.
A risk check refuses an entry whose worst possible loss exceeds 25% of capital, and any entry with stale or missing quotes."""
import json
import math
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .engine import ist, utc

API = "https://api.india.delta.exchange"
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)
RULE = {"weekday": 4, "hour": 18, "window_min": 10, "k_short": 0.8, "k_wing": 1.6, "hold_h": 18, "leverage": 7.5, "max_loss_pct": 25.0}
# Paper books. "fri-night" passed E2/E3. "weekend" (same entry, Sunday expiry) passed E4 on Delta's 13 months but LOST in the
# 5-year replay (E5 O1), so it no longer runs; its entry stays for its records. "trend-spread" passed E5 O3 (2026-11-11): the
# weekend timing, but only the side away from the 20-day trend is sold (a put spread above the average, a call spread below).
BOOKS = {"fri-night": {"name": "Friday night", "kind": "condor", "expiry_days": 1, "hold_h": 18, "state_key": "options", "test": "E2/E3: 57 Fridays, ~47%/yr, t 2.4"},
         "weekend": {"name": "Whole weekend", "kind": "condor", "expiry_days": 2, "hold_h": 42, "state_key": "options_weekend", "test": "E4: 50 weekends; retired after the E5 5-year replay"},
         "trend-spread": {"name": "Weekend trend spread", "kind": "spread", "expiry_days": 2, "hold_h": 42, "state_key": "options_trend",
                          "test": "E5 O3: 54 weekends, 96% won, t 3.8"}}
# R3 (owner's choice, 2026-11-11): the BTC Friday-night condor as one sleeve of the trend + carry + condor blend, sized
# 7.5 x 0.45 x 1.5 = 5.06x notional per leg on the shared $10,000 (edge/research/e12_r3_paper.py).
BOOKS["r3-btc-condor"] = {"name": "R3 · BTC Friday-night condor", "kind": "condor", "asset": "BTC", "leverage": 7.5 * 0.45 * 1.5, "expiry_days": 1,
                          "hold_h": 18, "state_key": "r3_btc_condor", "test": "BTC: E1 in-sample, 5-year replay t 1.87; R3 sleeve weight 0.45 x 1.5"}
# BE blend (owner, 2026-11-11: BTC and ETH only): both Friday-night condors, sized 7.5 x weight x 1.5 (edge/research/e13_be_paper.py).
BOOKS["be-btc-condor"] = {"name": "BTC Friday-night condor", "kind": "condor", "asset": "BTC", "leverage": 7.5 * 0.271 * 1.5, "expiry_days": 1, "hold_h": 18,
                          "state_key": "be_btc_condor", "test": "5-year replay t 1.87; BE weight 0.271 x 1.5"}
BOOKS["be-eth-condor"] = {"name": "ETH Friday-night condor", "kind": "condor", "asset": "ETH", "leverage": 7.5 * 0.175 * 1.5, "expiry_days": 1, "hold_h": 18,
                          "state_key": "be_eth_condor", "test": "E2/E3 passed on Delta's ETH prices; 5-year replay t 1.37; BE weight 0.175 x 1.5"}
# Owner, later on 2026-11-11: keep only the strong parts. BTC condor re-sized to 7.5 x 0.377 x 2.0; the ETH condor (Sharpe 0.58,
# losing 2023) was removed and stays here for its records only.
BOOKS["be-btc-condor"]["leverage"] = 7.5 * 0.377 * 2.0
BOOKS["be-btc-condor"]["test"] = "5-year replay t 1.87, no losing year; weight 0.377 x 2.0"
# Owner, 2026-11-11: add Analytical Trader's tighter ETH Friday strikes (ETH Consistent Strangles), WITH bought wings and small:
# short 0.57, wings 1.14 x DVOL x sqrt(T), 1.0x notional per leg (edge/research/e13_be_paper.py: real 13 months ~41%/yr for the
# whole account; 5-year replay of this part alone is weak, t 0.65).
BOOKS["be-eth-tight"] = {"name": "ETH Friday-night condor (tight)", "kind": "condor", "asset": "ETH", "leverage": 1.0, "expiry_days": 1, "hold_h": 18,
                         "k_short": 0.57, "k_wing": 1.14, "state_key": "be_eth_tight",
                         "test": "Their strikes + wings: Delta real ~41%/yr at 5x (t 2.42); 5-year replay t 0.65"}
RUNNING = ("be-btc-condor", "be-eth-tight")
QTY_STEP = {"ETH": 0.01, "BTC": 0.001}
# Delta's settlement index for options: the 30-minute average of these before expiry. (A product's own "settlement_price"
# is the OPTION's payout, "0" when it expires worthless; it must never be read as the underlying's price. Bug found
# 2026-11-21: both Friday condors were booked at BTC/ETH = 0, i.e. as full losses.)
INDEX = {"BTC": ".DEXBTUSD", "ETH": ".DEETHUSD"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "eth-condor-paper"}), timeout=20, context=CTX) as r:
        return json.loads(r.read())


def next_entry(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    t = d.replace(hour=RULE["hour"], minute=0, second=0, microsecond=0) + timedelta(days=(RULE["weekday"] - d.weekday()) % 7)
    if t.timestamp() + RULE["window_min"] * 60 <= now:
        t += timedelta(days=7)
    return t.timestamp()


def option_fee(px: float, qty: float, spot: float, rate: float, cap: float, gst: float) -> float:
    return (1 + gst) * min(rate * spot * qty, cap * px * qty)


def pick_strikes(listed: list[float], spot: float, hw_short: float, hw_wing: float) -> dict | None:
    """Nearest listed strikes to the targets; the wings must be strictly beyond the shorts."""
    if not listed:
        return None
    near = lambda x: min(listed, key=lambda k: abs(k - x))  # noqa: E731
    sc, sp = near(spot * (1 + hw_short)), near(spot * (1 - hw_short))
    wc = min((k for k in listed if k > sc), key=lambda k: abs(k - spot * (1 + hw_wing)), default=None)
    wp = min((k for k in listed if k < sp), key=lambda k: abs(k - spot * (1 - hw_wing)), default=None)
    if wc is None or wp is None or sc <= spot * 1.0005 or sp >= spot * 0.9995:
        return None
    return {"short_call": sc, "short_put": sp, "wing_call": wc, "wing_put": wp}


def pick_spread(listed: list[float], spot: float, hw_short: float, hw_wing: float, up: bool) -> dict | None:
    """Trend spread: above the 20-day average sell the put spread, below it the call spread (wing strictly further out)."""
    if not listed:
        return None
    near = lambda x: min(listed, key=lambda k: abs(k - x))  # noqa: E731
    if up:
        sp = near(spot * (1 - hw_short))
        wp = min((k for k in listed if k < sp), key=lambda k: abs(k - spot * (1 - hw_wing)), default=None)
        return None if wp is None or sp >= spot * 0.9995 else {"short_put": sp, "wing_put": wp}
    sc = near(spot * (1 + hw_short))
    wc = min((k for k in listed if k > sc), key=lambda k: abs(k - spot * (1 + hw_wing)), default=None)
    return None if wc is None or sc <= spot * 1.0005 else {"short_call": sc, "wing_call": wc}


def settle_value(legs: list[dict], price: float) -> float:
    """Value per unit owed (+) or received (-) at expiry for the short side of the condor."""
    v = 0.0
    for l in legs:
        intr = max(0.0, price - l["strike"]) if l["call"] else max(0.0, l["strike"] - price)
        v += intr if l["side"] == "sell" else -intr
    return v


class FridayCondor:
    def __init__(self, engine, capital: float = 10000.0, fetch=get, book: str = "fri-night"):
        self.eng, self.fetch, self.book = engine, fetch, book
        self.B = BOOKS[book]
        self.asset = self.B.get("asset", "ETH")
        self.lev = self.B.get("leverage", RULE["leverage"])
        self.step_q = QTY_STEP[self.asset]
        self.ks, self.kw = self.B.get("k_short", RULE["k_short"]), self.B.get("k_wing", RULE["k_wing"])
        self.gst = engine.C["gst"]
        self.state = {"cash": capital, "capital": capital, "last_entry_day": None, "pos": None, "events": []}
        r = None
        try:
            import sqlite3
            db = sqlite3.connect(str(engine.dbpath))
            r = db.execute("SELECT v FROM state WHERE k=?", (self.B["state_key"],)).fetchone()
            db.close()
        except Exception:
            pass
        if r:
            self.state.update(json.loads(r[0]))
        self.index_samples: list = []
        self.lock = threading.Lock()
        try:
            self.repair_bad_settlements()
        except Exception as e:  # network down: retried at the next start
            self._event(time.time(), "ERROR", f"settlement repair skipped: {type(e).__name__}: {e}")

    def repair_bad_settlements(self):
        """Re-settle trades that were booked at an impossible underlying price (the 2026-11-21 '0' bug), and correct cash."""
        import sqlite3
        db = sqlite3.connect(str(self.eng.dbpath), timeout=30)
        try:
            rows = db.execute("SELECT id, data FROM opt_trades").fetchall()
            for rid, data in rows:
                rec = json.loads(data)
                if rec.get("book") != self.book or not rec.get("spot") or rec.get("settle_price", 0) >= 0.5 * rec["spot"]:
                    continue
                price = self.index_settlement(rec["expiry_t"])
                if not price:
                    continue
                old_cash = (rec["credit"] - rec["owed"]) * rec["qty"] - rec["settle_fees"]
                owed, settle_fees, net, cash = self.settle_amounts(rec, price)
                self.state["cash"] += cash - old_cash
                was = rec["net"]
                rec |= {"settle_price": round(price, 2), "settle_source": "Delta index, 30-minute average before expiry (re-settled)",
                        "owed": round(owed, 4), "settle_fees": round(settle_fees, 4), "net": round(net, 2),
                        "net_pct": round(net / self.state["capital"] * 100, 3), "repaired": f"was booked at {self.asset} = 0, net {was:+.2f}"}
                db.execute("UPDATE opt_trades SET data=? WHERE id=?", (json.dumps(rec, default=str), rid))
                db.commit()
                self._event(time.time(), "REPAIR", f"the {rec['expiry_time'][:10]} expiry was wrongly settled at {self.asset} = 0 (net {was:+,.2f}); "
                            f"re-settled at {price:,.2f} from Delta's index: net {net:+,.2f}", {"net": net})
                self._save()
        finally:
            db.close()

    # ---- records (through the engine's single writer thread) ---------------------------------------------------------
    def _save(self):
        self.eng.out.put(("opt_state", (self.B["state_key"], json.dumps(self.state, default=str))))

    def _event(self, now, kind, text, extra=None):
        e = {"t": now, "time": utc(now), "time_ist": ist(now), "book": self.book, "kind": kind, "text": text} | (extra or {})
        self.state["events"] = (self.state["events"] + [e])[-200:]
        self.eng.out.put(("opt_event", e))

    # ---- market inputs ------------------------------------------------------------------------------------------------
    def spot(self) -> tuple[float | None, float]:
        i = self.eng.inst.get(f"{self.asset}USD")
        if i and i.index:
            return i.index, time.time() - i.index_rx
        try:
            return float(self.fetch(f"{API}/v2/tickers/{self.asset}USD")["result"]["spot_price"]), 0.0
        except Exception:
            return None, 1e9

    def dvol(self, entry_t: float) -> float | None:
        s = int(entry_t - 4 * 3600) * 1000
        rows = self.fetch(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={self.asset}&start_timestamp={s}&end_timestamp={int(entry_t) * 1000}&resolution=3600")["result"]["data"]
        want = int(entry_t // 3600 * 3600 - 3600) * 1000        # the hourly candle that closed at the entry hour
        for r in rows:
            if int(r[0]) == want:
                return float(r[4])
        return float(rows[-1][4]) if rows else None

    def trend_avg(self, now: float) -> float | None:
        """Average ETH index price at 00:00 UTC over the last 20 days (closes of the 20 completed daily candles)."""
        day0 = int(now // 86400 * 86400)
        rows = self.fetch(f"{API}/v2/history/candles?resolution=1d&symbol=.DE{self.asset}USD&start={day0 - 21 * 86400}&end={day0}")["result"] or []
        closes = [float(r["close"]) for r in rows if day0 - 20 * 86400 <= int(r["time"]) <= day0 - 86400]
        return sum(closes) / 20 if len(closes) == 20 else None

    def chain(self, code: str) -> dict:
        rows = self.fetch(f"{API}/v2/tickers?contract_types=call_options,put_options&underlying_asset_symbols={self.asset}")["result"]
        return {r["symbol"]: r for r in rows if r["symbol"].endswith("-" + code)}

    # ---- the loop -----------------------------------------------------------------------------------------------------
    def loop(self):
        while True:
            try:
                self.step(time.time())
            except Exception as e:
                self._event(time.time(), "ERROR", f"{type(e).__name__}: {e}")
            time.sleep(15)

    def step(self, now: float):
        with self.lock:
            p = self.state["pos"]
            if p:
                if now >= p["expiry_t"] - 1800:
                    s, _ = self.spot()
                    if s:
                        self.index_samples.append((now, s))
                if now >= p["expiry_t"] + 120:
                    self.settle(now)
                elif now - p.get("marked_t", 0) >= 30:
                    self.mark(now)
                return
            day = utc(now)[:10]
            entry = next_entry(now)
            if entry <= now < entry + RULE["window_min"] * 60 and self.state["last_entry_day"] != day:
                self.state["last_entry_day"] = day
                self.enter(now)
                self._save()

    def enter(self, now: float):
        spot, age = self.spot()
        if not spot or age > 10:
            return self._event(now, "NO_TRADE", f"{self.asset} index is stale or missing ({age:.0f} s old); skipped this Friday")
        vol = self.dvol(now)
        if not vol:
            return self._event(now, "NO_TRADE", f"Deribit {self.asset} DVOL unavailable; skipped this Friday")
        expiry = datetime.fromtimestamp(now, timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=self.B["expiry_days"])
        code = expiry.strftime("%d%m%y")
        ch = self.chain(code)
        T = self.B["hold_h"] / 8760
        hs, hw = self.ks * vol / 100 * math.sqrt(T), self.kw * vol / 100 * math.sqrt(T)
        calls = sorted(float(r["strike_price"]) for r in ch.values() if r["symbol"].startswith("C-"))
        puts = sorted(float(r["strike_price"]) for r in ch.values() if r["symbol"].startswith("P-"))
        trend = None
        if self.B["kind"] == "spread":
            avg = self.trend_avg(now)
            if not avg:
                return self._event(now, "NO_TRADE", "20-day ETH average unavailable (missing daily index candles); skipped this Friday")
            trend = {"avg20": round(avg, 2), "up": spot > avg}
            k = pick_spread(sorted(set(calls) & set(puts)), spot, hs, hw, spot > avg)
        else:
            k = pick_strikes(sorted(set(calls) & set(puts)), spot, hs, hw)
        if not k:
            return self._event(now, "NO_TRADE", f"no suitable listed strikes for the {code} expiry ({len(ch)} options listed)")
        sym = lambda kind, strike: f"{kind}-{self.asset}-{int(strike) if strike == int(strike) else strike}-{code}"  # noqa: E731
        spec = [x for x in (("short_call", "C", "sell"), ("short_put", "P", "sell"), ("wing_call", "C", "buy"), ("wing_put", "P", "buy")) if x[0] in k]
        legs = []
        for name, kind, side in spec:
            t = ch.get(sym(kind, k[name]))
            q = (t or {}).get("quotes") or {}
            px = float(q.get("best_bid") or 0) if side == "sell" else float(q.get("best_ask") or 0)
            if not t or px <= 0:
                return self._event(now, "NO_TRADE", f"no live {'bid' if side == 'sell' else 'ask'} for {sym(kind, k[name])}; strict fills forbid a mid-price fill")
            legs.append({"name": name, "symbol": t["symbol"], "strike": k[name], "call": kind == "C", "side": side, "px": px,
                         "mark": float(t.get("mark_price") or 0), "mark_iv": (t.get("quotes") or {}).get("mark_iv")})
        prod = self.fetch(f"{API}/v2/products/{legs[0]['symbol']}")["result"]
        rate, cap = float(prod["taker_commission_rate"]), float(prod["product_specs"].get("premium_commission_rate", 0.035))
        q = self.step_q
        qty = round(math.floor(self.lev * self.state["capital"] / spot / q) * q, 6)
        credit = sum(l["px"] * (1 if l["side"] == "sell" else -1) for l in legs)
        width = max(k.get("wing_call", 0) - k.get("short_call", 0), k.get("short_put", 0) - k.get("wing_put", 0))
        unit_fees = sum(option_fee(l["px"], 1.0, spot, rate, cap, self.gst) for l in legs)
        sized_down = None
        if credit > 0 and (width - credit + unit_fees) * qty > RULE["max_loss_pct"] / 100 * self.state["capital"]:
            # size down so the worst possible loss is exactly within the cap, rather than skipping the week
            fit = round(math.floor(RULE["max_loss_pct"] / 100 * self.state["capital"] / (width - credit + unit_fees) / q) * q, 6)
            sized_down = f"size cut from {qty:g} to {fit:g} {self.asset} so the worst possible loss stays within {RULE['max_loss_pct']}% of capital"
            qty = fit
        fees = sum(option_fee(l["px"], qty, spot, rate, cap, self.gst) for l in legs)
        max_loss = (width - credit) * qty + fees
        info = {"spot": spot, "dvol": vol, "code": code, "credit": round(credit, 4), "credit_bp": round(credit / spot * 1e4, 1), "qty": round(qty, 6),
                "max_loss": round(max_loss, 2), "max_loss_pct": round(max_loss / self.state["capital"] * 100, 2), "fees": round(fees, 4), "legs": legs,
                "sized_down": sized_down, "trend": trend}
        if credit <= 0:
            return self._event(now, "NO_TRADE", f"net credit {credit:.2f} is not positive at the live bid/ask", info)
        if qty < q or max_loss > RULE["max_loss_pct"] / 100 * self.state["capital"] * 1.001:
            return self._event(now, "NO_TRADE", f"worst possible loss {max_loss:,.0f} is over {RULE['max_loss_pct']}% of capital", info)
        self.state["cash"] -= fees
        self.state["pos"] = {"entry_t": now, "entry_time": utc(now), "entry_ist": ist(now), "expiry_t": expiry.timestamp(), "expiry_time": utc(expiry.timestamp()),
                             "rate": rate, "cap": cap, "marked_t": 0, "mark_pnl": None, **info}
        if trend:
            what = (f"{self.asset} {spot:,.2f} is {'above' if trend['up'] else 'below'} its 20-day average {trend['avg20']:,.2f}, so only the "
                    + (f"put side: sold the {k['short_put']:g} put and bought the {k['wing_put']:g} put" if trend["up"] else
                       f"call side: sold the {k['short_call']:g} call and bought the {k['wing_call']:g} call"))
        else:
            what = f"sold the {k['short_put']:g}/{k['short_call']:g} strangle and bought the {k['wing_put']:g}/{k['wing_call']:g} wings"
        self._event(now, "ENTER", f"{what}, "
                    f"{qty:g} {self.asset} each, for a net credit of {credit:.2f} per {self.asset} ({credit * qty:,.2f} total, {info['credit_bp']} bp of spot). "
                    f"{self.asset} {spot:,.2f}, DVOL {vol:.1f}. Worst possible loss {max_loss:,.2f} ({info['max_loss_pct']}% of capital); expires {expiry:%a %d %b} 17:30 IST."
                    + (f" ({sized_down})" if sized_down else ""), info)

    def mark(self, now: float):
        p = self.state["pos"]
        try:
            ch = self.chain(p["code"])
        except Exception:
            return
        cost_now = 0.0
        for l in p["legs"]:
            t = ch.get(l["symbol"])
            if not t:
                return
            l["mark_now"] = float(t.get("mark_price") or 0)
            cost_now += l["mark_now"] * (1 if l["side"] == "sell" else -1)
        p["mark_pnl"] = round((p["credit"] - cost_now) * p["qty"] - p["fees"], 4)
        p["marked_t"] = now
        self._save()

    def index_settlement(self, expiry_t: float) -> float | None:
        """Delta's settlement rule: mean of the index's 1-minute closes over the 30 minutes before expiry."""
        e = int(expiry_t)
        rows = self.fetch(f"{API}/v2/history/candles?resolution=1m&symbol={INDEX[self.asset]}&start={e - 1800}&end={e}")["result"] or []
        xs = [float(r["close"]) for r in rows if e - 1800 <= int(r["time"]) < e and float(r["close"]) > 0]
        return sum(xs) / len(xs) if len(xs) >= 25 else None

    def settle_amounts(self, p: dict, price: float) -> tuple[float, float, float, float]:
        """owed per unit, settlement fees, net P&L, and the cash that settlement adds."""
        owed = settle_value(p["legs"], price)
        settle_fees = sum(option_fee(max(0.0, (price - l["strike"]) if l["call"] else (l["strike"] - price)), p["qty"], price, p["rate"], p["cap"], self.gst)
                          for l in p["legs"])
        cash = (p["credit"] - owed) * p["qty"] - settle_fees
        return owed, settle_fees, cash - p["fees"], cash

    def settle(self, now: float):
        p = self.state["pos"]
        price, source = None, ""
        try:
            price = self.index_settlement(p["expiry_t"])
            source = "Delta index, 30-minute average before expiry"
        except Exception:
            price = None
        if price is None:
            xs = [s for t, s in self.index_samples if p["expiry_t"] - 1800 <= t <= p["expiry_t"] and s and s > 0]
            if len(xs) >= 10:
                price, source = sum(xs) / len(xs), "30-minute average of index samples taken live"
            elif now < p["expiry_t"] + 6 * 3600:
                return                      # wait for Delta's index candles
            else:
                s, _ = self.spot()
                if not s or s <= 0:
                    return
                price, source = s, "spot index 6 h after expiry (index candles unavailable)"
        if not price or price < 0.5 * p["spot"] or price > 2 * p["spot"]:
            return self._event(now, "ERROR", f"settlement price {price} rejected (entry spot {p['spot']}); will retry")
        owed, settle_fees, net, cash = self.settle_amounts(p, price)
        self.state["cash"] += cash
        rec = {k: v for k, v in p.items() if k not in ("legs",)} | {"book": self.book, "legs": p["legs"], "settle_price": round(price, 2), "settle_source": source,
                                                                    "owed": round(owed, 4), "settle_fees": round(settle_fees, 4), "net": round(net, 2),
                                                                    "net_pct": round(net / self.state["capital"] * 100, 3), "closed": utc(now)}
        self.eng.out.put(("opt_trade", rec))
        self.state["pos"] = None
        self.index_samples = []
        self._event(now, "SETTLE", f"expired with {self.asset} at {price:,.2f} ({source}): {'all legs worthless, full credit kept' if owed == 0 else f'paid {owed:.2f} per {self.asset}'}; "
                    f"net {net:+,.2f} ({rec['net_pct']:+.2f}% of capital) after fees", {"net": net})
        self._save()

    def view(self) -> dict:
        with self.lock:
            s = json.loads(json.dumps(self.state, default=str))
        now = time.time()
        s["next_entry_utc"] = utc(next_entry(now))
        s["next_entry_ist"] = datetime.fromtimestamp(next_entry(now) + 19800, timezone.utc).strftime("%a %d %b %H:%M IST")
        s["next_entry_in_h"] = round((next_entry(now) - now) / 3600, 1)
        s["rule"] = RULE | {"hold_h": self.B["hold_h"], "expiry_days": self.B["expiry_days"], "leverage": round(self.lev, 2), "asset": self.asset, "k_short": self.ks, "k_wing": self.kw}
        s["book"], s["book_name"], s["test"] = self.book, self.B["name"], self.B["test"]
        s["equity"] = s["cash"] + ((s["pos"] or {}).get("mark_pnl") or 0) + ((s["pos"] or {}).get("fees") or 0) if s["pos"] else s["cash"]
        return s
