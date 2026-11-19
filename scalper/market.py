"""Live market state per instrument: best bid/ask, trades, index, mark and funding, one-minute bars, and the features the
setups read. Everything is computed from data that existed before the decision (no look-ahead)."""
import math
import time
from collections import deque


def bp(a: float, b: float) -> float:
    return (a / b - 1) * 1e4


class Instrument:
    def __init__(self, symbol: str, index: str, contract: float, tick: float):
        self.symbol, self.index_sym, self.contract, self.tick = symbol, index, contract, tick
        self.bid = self.ask = None
        self.bq = self.aq = 0.0
        self.l1_rx = self.l1_ex = 0.0
        self.index, self.index_rx = None, 0.0
        self.mark = self.funding = self.oi = None
        self.bars: deque = deque(maxlen=1500)   # closed 1-minute bars, oldest first
        self.cur: dict | None = None
        self.lat: deque = deque(maxlen=2000)
        self.last_trade = None
        self.feats: dict = {}

    @property
    def mid(self):
        return (self.bid + self.ask) / 2 if self.bid and self.ask else None

    def spread_bp(self):
        return bp(self.ask, self.bid) if self.bid and self.ask else None

    # ---- events -------------------------------------------------------------------------------------------------
    def on_l1(self, m: dict, rx: float):
        self.bid, self.ask = float(m["best_bid"]), float(m["best_ask"])
        self.bq, self.aq = float(m.get("bid_qty") or 0), float(m.get("ask_qty") or 0)
        self.l1_rx, self.l1_ex = rx, (m.get("timestamp") or 0) / 1e6
        if self.l1_ex:
            self.lat.append(max(0.0, rx - self.l1_ex))

    def on_trade(self, m: dict, rx: float) -> tuple:
        px, size = float(m["price"]), float(m["size"]) * self.contract
        side = 1 if m.get("buyer_role") == "taker" else -1
        self.last_trade = (rx, px, size, side)
        return self.last_trade

    def bar_update(self, t: float, px: float, size: float = 0.0, side: int = 0) -> dict | None:
        """Feed a price; returns the bar that just closed, if a minute boundary passed."""
        m = int(t // 60 * 60)
        closed = None
        if self.cur and self.cur["t"] != m:
            closed = self.cur
            self.bars.append(closed)
            self.cur = None
        if not self.cur:
            self.cur = {"t": m, "o": px, "h": px, "l": px, "c": px, "v": 0.0, "buy": 0.0, "sell": 0.0, "n": 0, "live": True}
        b = self.cur
        b["h"], b["l"], b["c"] = max(b["h"], px), min(b["l"], px), px
        if size:
            b["v"] += size
            b["n"] += 1
            b["buy" if side > 0 else "sell"] += size
        return closed

    def latency_ms(self):
        xs = sorted(self.lat)
        if not xs:
            return {}
        q = lambda p: round(xs[int(p * (len(xs) - 1))] * 1000, 1)  # noqa: E731
        return {"p50": q(.5), "p90": q(.9), "p99": q(.99)}

    # ---- features (on each bar close) ---------------------------------------------------------------------------
    def compute(self, now: float | None = None) -> dict:
        B = list(self.bars)
        if len(B) < 250:
            self.feats = {"ready": False, "bars": len(B)}
            return self.feats
        c = [b["c"] for b in B]
        r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
        rv = lambda n: math.sqrt(sum(x * x for x in r[-n:]) / n * 525600) * 100  # noqa: E731  annualized %

        def atr(bars, n=14):
            a = None
            for i in range(1, len(bars)):
                tr = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]), abs(bars[i]["l"] - bars[i - 1]["c"]))
                a = tr if a is None else (a * (n - 1) + tr) / n
            return a
        tail = B[-300:]
        tail = tail[len(tail) % 5:]
        b5 = [{"h": max(x["h"] for x in g), "l": min(x["l"] for x in g), "c": g[-1]["c"]} for g in (tail[i:i + 5] for i in range(0, len(tail), 5))]
        last = B[-1]
        prev60, prev240 = B[-61:-1], B[-241:-1]
        vols = [b["v"] for b in prev60]
        day0 = int(last["t"] // 86400 * 86400)
        today = [b for b in B if b["t"] >= day0]
        vv = sum(b["v"] for b in today)
        vwap = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in today) / vv if vv else None
        ema = c[-240]
        k = 2 / 241
        for x in c[-240:]:
            ema = ema + k * (x - ema)
        rv15p = math.sqrt(sum(x * x for x in r[-16:-1]) / 15 * 525600) * 100   # compression measured before the last bar
        rv240 = rv(240)
        tot = last["buy"] + last["sell"]
        f = {
            "ready": True, "t": last["t"], "close": last["c"], "atr1": atr(B[-60:]), "atr5": atr(b5[-40:]) if len(b5) > 15 else None,
            "rv15": rv(15), "rv60": rv(60), "rv240": rv240, "compression": rv15p / rv240 if rv240 else None,
            "hi60": max(b["h"] for b in prev60), "lo60": min(b["l"] for b in prev60),
            "hi240": max(b["h"] for b in prev240), "lo240": min(b["l"] for b in prev240),
            "relvol": last["v"] / (sum(vols) / len(vols)) if sum(vols) else None,
            "delta": (last["buy"] - last["sell"]) / tot if tot and last.get("live") else None,
            "vwap": vwap, "ema240": ema, "ret1_bp": bp(c[-1], c[-2]),
        }
        f["regime"] = ("squeeze" if f["compression"] and f["compression"] < 0.6 else
                       "expansion" if f["rv15"] > 1.5 * rv240 else
                       "trend up" if c[-1] > ema and c[-1] > c[-60] else "trend down" if c[-1] < ema and c[-1] < c[-60] else "range")
        self.feats = f
        return f
