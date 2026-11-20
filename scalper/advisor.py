"""1-minute trade advisor with live learning (page /advisor). PAPER recommendations only: no orders, no API key.

On every closed 1-minute candle of BTCUSD and ETHUSD (Delta's own candles: the history replay and the live run read exactly
the same data, so the model never sees different inputs live than it learned on), in this order:
1. update the features incrementally (each uses only candles that have closed);
2. resolve open ideas: target, stop or time stop (when one candle touches both, the stop counts first; a target needs a
   trade through it), net of Delta's fees + GST, half the spread and slippage, and write why the idea worked or failed;
3. learn from every resolved candidate, not only the ones it picked (so its own choices cannot fool it): one online logistic
   model per setup family scores how far toward the target price is likely to get, from 14 context features;
4. scan five setup families for new candidates, score each one, and record it as RECOMMENDED, UNPROVEN, BLOCKED or SKIPPED,
   with entry, stop, target, size and the reasons.

Honesty rules (skill btc-eth-scalping-agent §0, §4, §9):
- targets are at least 3x the worst round-trip cost, so no 1-5 minute scalps (they lose to Delta's fees);
- a family is RECOMMENDED only when the model's own earlier picks (each scored before its outcome was known) number at least
  100, with net > 0, t >= 2 and profit factor >= 1.3, both overall and over its latest 100; until then a pick is UNPROVEN;
- learning changes model weights only; never risk limits, costs, the trust gate or the rules below. One trade moves the
  weights a little; it never rewrites a rule.
Version L1 was written on 2026-11-19 before its replay results were seen; changing families, features or thresholds after
seeing results makes a new version (L2) with a fresh record, and the number of versions tried is shown on the page."""
import json
import math
import os
import queue
import sqlite3
import ssl
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://api.india.delta.exchange"
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)
SYMBOLS = ("BTCUSD", "ETHUSD")
VERSION, VERSIONS_TRIED = "L1", 1
HISTORY_DAYS = 180
WARM_BARS = 300
SPREAD_BP = {"BTCUSD": 0.07, "ETHUSD": 0.25}        # live medians (backtest.py); used where no live quote exists
CONTRACT = {"BTCUSD": 0.001, "ETHUSD": 0.01}
MIN_EDGE_R = 0.10                                   # a pick needs expected net >= +0.10 R after costs
COOLDOWN_MIN = 30                                   # one candidate per family and coin per 30 minutes
TRUST = {"min_picks": 100, "min_t": 2.0, "min_pf": 1.3}
RISK_PCT = 0.25                                     # skill §8: 0.25% of equity per idea

FAMILIES = {
    "breakout": {"name": "Range breakout", "min_stop_bp": 25, "stop_atr5": 1.2, "target_r": 2.0, "time_min": 90,
                 "hypothesis": "A close beyond the 1-hour range traps traders positioned for the range; their stops push price further."},
    "sweep": {"name": "Sweep and reclaim", "min_stop_bp": 25, "stop_atr5": 0.3, "target_r": 2.0, "time_min": 60,
              "hypothesis": "A push through the 4-hour high or low that closes back inside was a stop hunt; the traders who chased it "
                            "are trapped and price returns into the range."},
    "flush": {"name": "Flush reversal", "min_stop_bp": 25, "stop_atr5": 0.2, "target_r": 2.0, "time_min": 45,
              "hypothesis": "A one-minute flush of 3x normal range on 4x volume is forced liquidation; once the next minute reverses, the "
                            "forced flow is done and price snaps back."},
    "pullback": {"name": "Trend pullback", "min_stop_bp": 30, "stop_atr5": 1.5, "target_r": 2.0, "time_min": 120,
                 "hypothesis": "In a 4-hour trend, a dip to the 1-hour average that is bought (or a rally that is sold) is trend "
                               "traders adding; price resumes the trend."},
    "open": {"name": "Session-open drive", "min_stop_bp": 30, "stop_atr5": 1.5, "target_r": 2.0, "time_min": 60,
             "hypothesis": "New participants arrive at the Tokyo, London and New York opens; a strong first 15 minutes tends to "
                           "continue for the next hour."},
}
OPENS = (("Tokyo", "Asia/Tokyo", 9, 0), ("London", "Europe/London", 8, 0), ("New York", "America/New_York", 9, 30))
FEATS = ("trend", "mom60", "vwap", "compress", "relvol", "vol_level", "range_pos", "bar_dir", "stop_size",
         "asia", "europe", "us", "weekend", "eth")
FEAT_LABEL = {"trend": "4-hour trend in its favour", "mom60": "1-hour momentum in its favour", "vwap": "distance from VWAP in its favour",
              "compress": "volatility squeeze (low = squeezed)", "relvol": "volume vs normal", "vol_level": "volatility level",
              "range_pos": "position in the 4-hour range", "bar_dir": "signal candle in its favour", "stop_size": "stop width",
              "asia": "Asia session", "europe": "Europe session", "us": "US session", "weekend": "weekend", "eth": "ETH (vs BTC)"}

FEAT_OFF = {"asia": "outside Asia hours", "europe": "outside Europe hours", "us": "outside US hours", "weekend": "weekday", "eth": "BTC (vs ETH)"}


def feat_name(k: str, x: float) -> str:
    """Name an input by what was true: a yes/no input that was 'no' is named as such (e.g. 'weekday', not 'weekend')."""
    return FEAT_OFF[k] if k in FEAT_OFF and x == 0 else FEAT_LABEL[k]


def bp(a: float, b: float) -> float:
    return (a / b - 1) * 1e4


def utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def ist(t: float) -> str:
    return datetime.fromtimestamp(t + 19800, timezone.utc).strftime("%d %b %H:%M IST")


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def get(path: str):
    for k in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "scalper-advisor"}), timeout=30, context=CTX) as r:
                return json.loads(r.read())
        except Exception:
            if k == 4:
                raise
            time.sleep(1 + 2 * k)


def candles(sym: str, start: int, end: int) -> list:
    """Closed 1-minute candles [t, o, h, l, c, v] with start <= t and t + 60 <= end, oldest first."""
    rows, hi = {}, end
    while hi > start:
        lo = max(start, hi - 3990 * 60)
        for r in get(f"/v2/history/candles?resolution=1m&symbol={sym}&start={lo}&end={hi}")["result"] or []:
            rows[int(r["time"])] = [float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"] or 0)]
        hi = lo
    return [[t] + rows[t] for t in sorted(rows) if start <= t and t + 60 <= end]


# ---- features ---------------------------------------------------------------------------------------------------------
class Features:
    """Incremental features on closed 1-minute candles. Window highs/lows/volumes cover the candles BEFORE the current one;
    returns, ATR, VWAP and EMAs include the current candle, which has closed. Identical code in the replay and live."""

    def __init__(self):
        self.n = 0
        self.closes: deque = deque(maxlen=241)
        self.H: deque = deque(maxlen=240)
        self.L: deque = deque(maxlen=240)
        self.V: deque = deque(maxlen=60)
        self.R2: deque = deque(maxlen=241)
        self.bars: deque = deque(maxlen=720)
        self.atr1 = self.atr5 = self.ema60 = self.ema240 = None
        self.b5 = None                                  # [bucket, h, l, c]
        self.c5 = None                                  # previous 5-minute close
        self.day, self.pv, self.vv = None, 0.0, 0.0
        self.prev_relvol = None
        self.last = None

    def update(self, bar) -> dict | None:
        t, o, h, l, c, v = bar
        prev_c = self.closes[-1] if self.closes else None
        r = math.log(c / prev_c) if prev_c else 0.0
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c)) if prev_c else h - l
        self.atr1 = tr if self.atr1 is None else (self.atr1 * 13 + tr) / 14
        k5 = t // 300
        if self.b5 and self.b5[0] != k5:                # the previous 5-minute bar is complete
            _, bh, bl, bc = self.b5
            tr5 = max(bh - bl, abs(bh - self.c5), abs(bl - self.c5)) if self.c5 else bh - bl
            self.atr5 = tr5 if self.atr5 is None else (self.atr5 * 13 + tr5) / 14
            self.c5, self.b5 = bc, None
        self.b5 = [k5, h, l, c] if not self.b5 else [k5, max(self.b5[1], h), min(self.b5[2], l), c]
        day = t // 86400
        if day != self.day:
            self.day, self.pv, self.vv = day, 0.0, 0.0
        self.pv += (h + l + c) / 3 * v
        self.vv += v
        self.ema60 = c if self.ema60 is None else self.ema60 + 2 / 61 * (c - self.ema60)
        self.ema240 = c if self.ema240 is None else self.ema240 + 2 / 241 * (c - self.ema240)
        self.R2.append(r * r)
        self.closes.append(c)
        f = None
        self.n += 1
        if self.n >= WARM_BARS and self.atr5 and len(self.H) == 240 and len(self.R2) == 241:
            r2 = list(self.R2)
            ann = lambda xs: math.sqrt(sum(xs) / len(xs) * 525600) * 100  # noqa: E731  annualized %
            rv240, rv15p = ann(r2[-240:]), ann(r2[-16:-1])
            H, L = list(self.H), list(self.L)
            vmean = sum(self.V) / len(self.V)
            relvol = v / vmean if vmean else 1.0
            pb = self.bars[-1]
            f = {"t": t, "tc": t + 60, "o": o, "h": h, "l": l, "c": c, "v": v, "atr1": self.atr1, "atr5": self.atr5,
                 "atr5_bp": self.atr5 / c * 1e4, "rv15": ann(r2[-15:]), "rv60": ann(r2[-60:]), "rv240": rv240,
                 "compression": rv15p / rv240 if rv240 else 1.0, "hi60": max(H[-60:]), "lo60": min(L[-60:]),
                 "hi240": max(H), "lo240": min(L), "relvol": relvol, "relvol_prev": self.prev_relvol or 1.0,
                 "vwap": self.pv / self.vv if self.vv else c, "ema60": self.ema60, "ema240": self.ema240,
                 "mom60_bp": bp(c, self.closes[-61]), "c15": self.closes[-16], "prev": {"o": pb[1], "h": pb[2], "l": pb[3], "c": pb[4], "v": pb[5]},
                 "low6": min(min(L[-5:]), l), "high6": max(max(H[-5:]), h)}
            f["regime"] = ("squeeze" if f["compression"] < 0.6 else "expansion" if f["rv15"] > 1.5 * rv240 else
                           "trend up" if c > self.ema240 and f["mom60_bp"] > 0 else "trend down" if c < self.ema240 and f["mom60_bp"] < 0 else "range")
            self.prev_relvol = relvol
        elif self.V:
            vmean = sum(self.V) / len(self.V)
            self.prev_relvol = v / vmean if vmean else 1.0
        self.H.append(h)
        self.L.append(l)
        self.V.append(v)
        self.bars.append(bar)
        self.last = f
        return f


_opens_cache: dict = {}


def session_open(tc: int) -> str | None:
    """Name of the market whose open was exactly 15 minutes before this candle's close (DST handled by zoneinfo)."""
    day = datetime.fromtimestamp(tc - 900, timezone.utc).date()
    if day not in _opens_cache:
        out = {}
        for name, tz, hh, mm in OPENS:
            for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
                local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(tz))
                if local.weekday() < 5:
                    out[int(local.timestamp())] = name
        if len(_opens_cache) > 400:
            _opens_cache.clear()
        _opens_cache[day] = out
    return _opens_cache[day].get(tc - 900)


def scan(sym: str, f: dict) -> list[dict]:
    """The five families' entry rules (fixed for version L1). Each candidate has a side, stop distance, reasons and what
    would prove it wrong. No filter beyond the rule itself: the model learns which conditions make each one pay."""
    out, c, a5 = [], f["c"], f["atr5_bp"]
    coin = sym[:3]
    vol = f"volume {f['relvol']:.1f}x normal"
    ctx = f"Regime: {f['regime']}; price {'above' if c > f['vwap'] else 'below'} VWAP {f['vwap']:,.2f}; {vol}."

    def add(fam, side, stop_bp, why, invalid):
        p = FAMILIES[fam]
        out.append({"family": fam, "side": side, "stop_bp": max(p["min_stop_bp"], stop_bp), "why": why + " " + ctx, "invalidation": invalid})

    if c > f["hi60"] and bp(c, f["hi60"]) >= 5:
        add("breakout", 1, 1.2 * a5, f"{coin} closed {bp(c, f['hi60']):.0f} bp above its 1-hour high {f['hi60']:,.2f} "
            f"(short-term volatility {f['compression']:.2f}x the 4-hour level).", f"a close back below {f['hi60']:,.2f}, or no follow-through in 90 min")
    elif c < f["lo60"] and bp(f["lo60"], c) >= 5:
        add("breakout", -1, 1.2 * a5, f"{coin} closed {bp(f['lo60'], c):.0f} bp below its 1-hour low {f['lo60']:,.2f} "
            f"(short-term volatility {f['compression']:.2f}x the 4-hour level).", f"a close back above {f['lo60']:,.2f}, or no follow-through in 90 min")
    if f["low6"] < f["lo240"] and bp(f["lo240"], f["low6"]) >= 8 and c > f["lo240"] and c > f["o"]:
        add("sweep", 1, bp(c, f["low6"]) + 0.3 * a5, f"{coin} dipped {bp(f['lo240'], f['low6']):.0f} bp under its 4-hour low "
            f"{f['lo240']:,.2f} to {f['low6']:,.2f} and closed back above it on a rising candle.", f"a new low under {f['low6']:,.2f}")
    if f["high6"] > f["hi240"] and bp(f["high6"], f["hi240"]) >= 8 and c < f["hi240"] and c < f["o"]:
        add("sweep", -1, bp(f["high6"], c) + 0.3 * a5, f"{coin} spiked {bp(f['high6'], f['hi240']):.0f} bp over its 4-hour high "
            f"{f['hi240']:,.2f} to {f['high6']:,.2f} and closed back below it on a falling candle.", f"a new high over {f['high6']:,.2f}")
    p = f["prev"]
    rng = p["h"] - p["l"]
    if rng >= 3 * f["atr1"] and f["relvol_prev"] >= 4:
        side = 1 if p["c"] < p["o"] else -1
        if side * (c - f["o"]) > 0 and side * (c - p["c"]) / rng >= 0.25:
            ext = p["l"] if side > 0 else p["h"]
            add("flush", side, abs(bp(c, ext)) + 0.2 * a5, f"{coin} {'fell' if side > 0 else 'jumped'} {abs(bp(p['c'], p['o'])):.0f} bp in one minute "
                f"({rng / f['atr1']:.1f}x the normal range, {f['relvol_prev']:.1f}x volume), which looks like forced {'selling' if side > 0 else 'buying'}; "
                f"this minute took back {side * (c - p['c']) / rng * 100:.0f}% of it.", f"a break of the flush extreme {ext:,.2f}")
    up = c > f["ema240"] and f["ema60"] > f["ema240"] and f["mom60_bp"] > 0
    dn = c < f["ema240"] and f["ema60"] < f["ema240"] and f["mom60_bp"] < 0
    if up and f["l"] <= f["ema60"] < c and c > f["o"]:
        add("pullback", 1, 1.5 * a5, f"{coin} is in a 4-hour uptrend (above its 4-hour average {f['ema240']:,.2f}); this candle dipped to the "
            f"1-hour average {f['ema60']:,.2f} and closed back above it, up.", f"a close under the 4-hour average {f['ema240']:,.2f}")
    if dn and f["h"] >= f["ema60"] > c and c < f["o"]:
        add("pullback", -1, 1.5 * a5, f"{coin} is in a 4-hour downtrend (below its 4-hour average {f['ema240']:,.2f}); this candle rallied to the "
            f"1-hour average {f['ema60']:,.2f} and closed back below it, down.", f"a close over the 4-hour average {f['ema240']:,.2f}")
    mkt = session_open(f["tc"])
    if mkt:
        move = bp(c, f["c15"])
        if abs(move) >= max(15.0, a5):
            side = 1 if move > 0 else -1
            add("open", side, 1.5 * a5, f"The {mkt} open 15 minutes ago drove {coin} {move:+.0f} bp ({abs(move) / a5:.1f}x a normal 5-minute range).",
                f"a move back past the opening price {f['c15']:,.2f}")
    return out


def context(sym: str, f: dict, side: int, stop_bp: float) -> dict:
    """The model's 14 inputs, all signed so that positive means 'in the idea's favour' where that makes sense."""
    hour = datetime.fromtimestamp(f["tc"], timezone.utc)
    h = hour.hour
    rng = f["hi240"] - f["lo240"]
    clip = lambda x, a=10.0: max(-a, min(a, x))  # noqa: E731
    return {"trend": float(side if f["c"] > f["ema240"] else -side), "mom60": clip(side * f["mom60_bp"] / f["atr5_bp"]),
            "vwap": clip(side * (f["c"] - f["vwap"]) / f["atr5"]), "compress": math.log(max(0.05, f["compression"])),
            "relvol": math.log1p(f["relvol"]), "vol_level": math.log(max(1.0, f["rv240"])),
            "range_pos": side * (((f["c"] - f["lo240"]) / rng if rng else 0.5) - 0.5), "bar_dir": float(side if f["c"] > f["o"] else -side if f["c"] < f["o"] else 0),
            "stop_size": math.log(stop_bp / 25), "asia": float(h < 7), "europe": float(7 <= h < 13), "us": float(13 <= h < 21),
            "weekend": float(hour.weekday() >= 5), "eth": float(sym == "ETHUSD")}


# ---- the learner --------------------------------------------------------------------------------------------------------
class Normalizer:
    """Running mean and variance per input (Welford), updated when a candidate is seen (its inputs are known then)."""

    def __init__(self, d=None):
        self.s = d or {k: [0, 0.0, 0.0] for k in FEATS}

    def push(self, x: dict):
        for k in FEATS:
            n, m, M2 = self.s[k]
            n += 1
            dm = x[k] - m
            m += dm / n
            self.s[k] = [n, m, M2 + dm * (x[k] - m)]

    def z(self, x: dict) -> dict:
        out = {}
        for k in FEATS:
            n, m, M2 = self.s[k]
            sd = math.sqrt(M2 / (n - 1)) if n > 1 else 1.0
            out[k] = max(-5.0, min(5.0, (x[k] - m) / sd)) if sd > 1e-9 else 0.0
        return out


class Model:
    """Online logistic model for one family. Label: how far toward the target price went, 0 = full stop, 1 = full target
    (time stops land in between), so expected gross R = score x (target_r + 1) - 1. AdaGrad steps with a small L2 pull
    toward zero. It starts at exactly 'no edge before costs', so an unlearned family can never look attractive."""
    LR, L2 = 0.05, 1e-3

    def __init__(self, target_r: float, d=None):
        p0 = 1 / (1 + target_r)
        d = d or {"b": math.log(p0 / (1 - p0)), "w": {k: 0.0 for k in FEATS}, "G": {k: 1e-6 for k in FEATS + ("b",)}, "n": 0}
        self.b, self.w, self.G, self.n = d["b"], d["w"], d["G"], d["n"]

    def to_dict(self):
        return {"b": self.b, "w": self.w, "G": self.G, "n": self.n}

    def score(self, z: dict) -> float:
        return sigmoid(self.b + sum(self.w[k] * z[k] for k in FEATS))

    def contributions(self, z: dict) -> list:
        return sorted(((k, self.w[k] * z[k]) for k in FEATS), key=lambda kv: -abs(kv[1]))

    def learn(self, z: dict, y: float) -> dict:
        before = dict(self.w)
        g = self.score(z) - y
        for k in FEATS:
            gk = g * z[k] + self.L2 * self.w[k]
            self.G[k] += gk * gk
            self.w[k] -= self.LR / math.sqrt(self.G[k]) * gk
        self.G["b"] += g * g
        self.b -= self.LR / math.sqrt(self.G["b"]) * g
        self.n += 1
        return {k: self.w[k] - before[k] for k in FEATS}


class Stats:
    """Running sums of net R: n, mean, t, profit factor; plus the latest 100."""

    def __init__(self, d=None):
        d = d or {"n": 0, "s": 0.0, "ss": 0.0, "win": 0.0, "loss": 0.0, "hits": 0, "last": []}
        self.d = d

    def add(self, r: float, hit: bool):
        d = self.d
        d["n"] += 1
        d["s"] += r
        d["ss"] += r * r
        d["win" if r > 0 else "loss"] += abs(r)
        d["hits"] += int(hit)
        d["last"] = (d["last"] + [round(r, 4)])[-100:]

    @staticmethod
    def summary_of(xs: list) -> dict:
        n = len(xs)
        if not n:
            return {"n": 0}
        m = sum(xs) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
        win, loss = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
        return {"n": n, "mean_r": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None,
                "pf": round(win / loss, 2) if loss else None, "win_pct": round(sum(1 for x in xs if x > 0) / n * 100, 1)}

    def summary(self) -> dict:
        d = self.d
        n = d["n"]
        if not n:
            return {"n": 0}
        m = d["s"] / n
        var = (d["ss"] - n * m * m) / (n - 1) if n > 1 else 0.0
        sd = math.sqrt(max(var, 0.0))
        return {"n": n, "mean_r": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None,
                "pf": round(d["win"] / d["loss"], 2) if d["loss"] else None, "target_pct": round(d["hits"] / n * 100, 1),
                "sum_r": round(d["s"], 2), "last100": self.summary_of(d["last"])}


class Book:
    """The virtual account that follows the model's picks at 0.25% risk each, under the skill's §8 limits that apply to
    recommendations (open ideas, ideas per day, daily loss, losing streaks). It never sizes up after losses."""

    def __init__(self, limits: dict, d=None):
        self.L = limits
        self.d = d or {"equity": 10000.0, "peak": 10000.0, "max_dd_pct": 0.0, "day": None, "day_start": 10000.0, "trades_day": 0,
                       "consec": 0, "consec_day": 0, "pause_until": 0, "open": 0, "curve": [], "n": 0}

    def _roll(self, t):
        d, day = self.d, utc(t)[:10]
        if d["day"] != day:
            d.update(day=day, day_start=d["equity"], trades_day=0, consec_day=0)

    def block(self, t) -> str | None:
        self._roll(t)
        d, L = self.d, self.L
        if d["open"] >= L["max_positions"]:
            return f"{L['max_positions']} ideas already open"
        if d["trades_day"] >= L["max_trades_per_day"]:
            return f"{L['max_trades_per_day']} ideas already today"
        if d["equity"] <= d["day_start"] * (1 - L["daily_loss_pct"] / 100):
            return f"daily loss limit {L['daily_loss_pct']}% reached"
        if t < d["pause_until"]:
            return f"paused after {L['consecutive_losses_pause']} losses in a row until {utc(d['pause_until'])} UTC"
        if d["consec_day"] >= L["consecutive_losses_day"]:
            return f"{L['consecutive_losses_day']} losses today"
        return None

    def open(self, t) -> float:
        self.d["open"] += 1
        self.d["trades_day"] += 1
        return self.d["equity"] * RISK_PCT / 100

    def close(self, t, risk_usd: float, net_r: float):
        d = self.d
        self._roll(t)
        d["open"] = max(0, d["open"] - 1)
        d["equity"] += risk_usd * net_r
        d["n"] += 1
        d["peak"] = max(d["peak"], d["equity"])
        d["max_dd_pct"] = max(d["max_dd_pct"], (1 - d["equity"] / d["peak"]) * 100)
        if net_r < 0:
            d["consec"] += 1
            d["consec_day"] += 1
            if d["consec"] >= self.L["consecutive_losses_pause"]:
                d["pause_until"] = t + self.L["pause_minutes"] * 60
                d["consec"] = 0
        else:
            d["consec"] = 0
        d["curve"].append([int(t), round(d["equity"], 2)])
        if len(d["curve"]) > 5000:
            d["curve"] = d["curve"][::2]


# ---- the advisor ----------------------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS recs (id TEXT PRIMARY KEY, t INTEGER, symbol TEXT, family TEXT, status TEXT, source TEXT, outcome TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS recs_t ON recs (source, t);
CREATE TABLE IF NOT EXISTS learnlog (id INTEGER PRIMARY KEY, t INTEGER, family TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
"""


class Advisor:
    def __init__(self, root: Path, fees: dict, limits: dict, quote=None, slippage_bp: float = 1.0, events=(), fetch=candles):
        """fees: {sym: {"maker": bp, "taker": bp}} incl. GST. quote(sym) -> (bid, ask, age_s) or None, for live entries."""
        self.dir = Path(root) / "advisor"
        self.dir.mkdir(exist_ok=True)
        self.dbpath = self.dir / "advisor.sqlite"
        self.fees, self.limits, self.quote, self.slip, self.fetch = fees, limits, quote, slippage_bp, fetch
        self.events = [datetime.fromisoformat(e.replace("Z", "+00:00")).timestamp() for e in events]
        self.lock = threading.RLock()
        self.out: queue.Queue = queue.Queue()
        self.feat = {s: Features() for s in SYMBOLS}
        self.norm = Normalizer()
        self.models = {k: Model(p["target_r"]) for k, p in FAMILIES.items()}
        self.all = {k: Stats() for k in FAMILIES}            # every resolved candidate
        self.picks = {k: Stats() for k in FAMILIES}          # the model's picks, scored before the outcome (prequential)
        self.live_picks = {k: Stats() for k in FAMILIES}
        self.calib = {k: [[0, 0.0, 0.0] for _ in range(10)] for k in FAMILIES}   # per score band: n, predicted R, realized R
        self.books = {"history": Book(limits), "live": Book(limits)}
        self.weights_day: dict = {}                          # weights at the start of each UTC day, for "what changed"
        self.last_t = {s: 0 for s in SYMBOLS}
        self.cool: dict = {}
        self.open: dict = {}                                 # id -> rec, ideas not yet resolved
        self.recent: deque = deque(maxlen=400)               # latest live recs, for the page
        self.log: deque = deque(maxlen=200)
        self.seq = 0
        self.phase, self.progress, self.live_since, self.error = "starting", 0.0, None, None
        self._db = None
        self._bar_cache: dict = {}
        db = sqlite3.connect(str(self.dbpath))
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.commit()
        self._restore(db)
        db.close()

    # ---- persistence --------------------------------------------------------------------------------------------------
    def _restore(self, db):
        r = db.execute("SELECT v FROM state WHERE k='advisor'").fetchone()
        if not r:
            return
        s = json.loads(r[0])
        if s.get("version") != VERSION:
            return
        self.norm = Normalizer(s["norm"])
        self.models = {k: Model(FAMILIES[k]["target_r"], s["models"][k]) for k in FAMILIES}
        self.all = {k: Stats(s["all"][k]) for k in FAMILIES}
        self.picks = {k: Stats(s["picks"][k]) for k in FAMILIES}
        self.live_picks = {k: Stats(s["live_picks"][k]) for k in FAMILIES}
        self.calib = s["calib"]
        self.books = {k: Book(self.limits, v) for k, v in s["books"].items()}
        self.weights_day, self.last_t, self.seq, self.live_since = s["weights_day"], s["last_t"], s["seq"], s.get("live_since")
        self.cool = {tuple(k.split("|")): v for k, v in s["cool"].items()}
        self.open = s["open"]                       # open ideas with their path, as of the same moment as last_t
        for (d,) in db.execute("SELECT data FROM recs WHERE source != 'history' ORDER BY t DESC LIMIT 400"):
            rec = json.loads(d)
            self.recent.appendleft(self.open.get(rec["id"], rec))
        for (d,) in db.execute("SELECT data FROM learnlog ORDER BY id DESC LIMIT 200"):
            self.log.appendleft(json.loads(d))

    def _state(self) -> str:
        return json.dumps({"version": VERSION, "norm": self.norm.s, "models": {k: m.to_dict() for k, m in self.models.items()},
                           "all": {k: v.d for k, v in self.all.items()}, "picks": {k: v.d for k, v in self.picks.items()},
                           "live_picks": {k: v.d for k, v in self.live_picks.items()}, "calib": self.calib,
                           "books": {k: b.d for k, b in self.books.items()}, "weights_day": self.weights_day, "last_t": self.last_t,
                           "seq": self.seq, "live_since": self.live_since, "cool": {"|".join(k): v for k, v in self.cool.items()},
                           "open": self.open}, default=str)

    def writer(self):
        db = sqlite3.connect(str(self.dbpath), timeout=10)
        while True:
            items = [self.out.get()]
            while not self.out.empty() and len(items) < 5000:
                items.append(self.out.get())
            self._write_items(db, items)

    def flush(self):
        """Batch mode (cloud.py): write everything queued now, on this thread."""
        db = sqlite3.connect(str(self.dbpath), timeout=10)
        items = []
        while not self.out.empty():
            items.append(self.out.get())
        self._write_items(db, items)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()

    def _write_items(self, db, items):
        try:
            for kind, x in items:
                if kind == "rec":
                    db.execute("INSERT OR REPLACE INTO recs VALUES (?,?,?,?,?,?,?,?)", (x["id"], x["t"], x["symbol"], x["family"], x["status"],
                                                                                  x["source"], x.get("outcome"), json.dumps({k: v for k, v in x.items() if k != "z"}, default=str)))
                elif kind == "log":
                    db.execute("INSERT INTO learnlog (t, family, data) VALUES (?,?,?)", (x["t"], x["family"], json.dumps(x, default=str)))
                elif kind == "state":
                    db.execute("INSERT OR REPLACE INTO state VALUES ('advisor', ?)", (x,))
            db.commit()
        except Exception as e:  # reported, never silent
            with open(self.dir / "errors.log", "a") as fh:
                fh.write(f"{utc(time.time())} WRITE FAILED: {type(e).__name__}: {e}\n")

    # ---- one closed candle --------------------------------------------------------------------------------------------
    def on_bar(self, sym: str, bar: list, source: str, warm_only: bool = False):
        """source: 'history' (the first replay), 'catch-up' (candles missed while the app was off), 'cloud' (cloud.py) or 'live'."""
        f = self.feat[sym].update(bar)
        self.last_t[sym] = max(self.last_t[sym], int(bar[0]))
        if warm_only:
            return
        for rec in [r for r in self.open.values() if r["symbol"] == sym]:
            self._advance(rec, bar, f)
        if f:
            day = utc(f["tc"])[:10]
            if day not in self.weights_day:
                self.weights_day[day] = {k: dict(m.w) for k, m in self.models.items()}
                for old in sorted(self.weights_day)[:-8]:
                    del self.weights_day[old]
            for c in scan(sym, f):
                key = (c["family"], sym)
                if f["tc"] - self.cool.get(key, 0) < COOLDOWN_MIN * 60:
                    continue
                self.cool[key] = f["tc"]
                self._new(sym, c, f, source)

    def _costs(self, sym: str, spread_bp: float) -> dict:
        fee = self.fees[sym]
        taker_leg = fee["taker"] + spread_bp / 2 + self.slip
        return {"entry": taker_leg, "exit_target": fee["maker"], "exit_taker": taker_leg, "round_trip_worst": 2 * taker_leg}

    def trusted(self, fam: str) -> tuple[bool, str]:
        s = self.picks[fam].summary()
        if s["n"] < TRUST["min_picks"]:
            return False, f"{s['n']} of {TRUST['min_picks']} scored picks so far"
        last = s["last100"]
        ok = (s["mean_r"] > 0 and (s["t"] or 0) >= TRUST["min_t"] and (s["pf"] or 0) >= TRUST["min_pf"] and last.get("mean_r", -1) > 0)
        return ok, (f"picks: {s['n']}, mean {s['mean_r']:+.3f} R, t {s['t']}, PF {s['pf']}; latest 100 mean {last.get('mean_r', 0):+.3f} R"
                    + ("" if ok else f" (needs mean > 0, t >= {TRUST['min_t']}, PF >= {TRUST['min_pf']}, latest 100 > 0)"))

    def _new(self, sym: str, c: dict, f: dict, source: str):
        fam, side, R = c["family"], c["side"], c["stop_bp"]
        p, m = FAMILIES[fam], self.models[fam]
        x = context(sym, f, side, R)
        self.norm.push(x)
        z = self.norm.z(x)
        score = m.score(z)
        T = p["target_r"]
        exp_gross = score * (T + 1) - 1
        live = source == "live"
        q = self.quote(sym) if (live and self.quote) else None
        fresh = bool(q and q[0] and q[1] and q[2] <= self.limits["max_l1_age_s"])   # a frozen feed (e.g. after sleep) is never a price
        spread = bp(q[1], q[0]) if fresh else SPREAD_BP[sym]
        cost = self._costs(sym, spread)
        exp_cost_r = (cost["entry"] + score * cost["exit_target"] + (1 - score) * cost["exit_taker"]) / R
        exp_net = exp_gross - exp_cost_r
        self.seq += 1
        rid = f"A-{utc(f['tc'])[:10]}-{self.seq:06d}"
        contrib = m.contributions(z)
        helps = [feat_name(k, x[k]) for k, v in contrib if v > 0.02][:3]
        hurts = [feat_name(k, x[k]) for k, v in contrib if v < -0.02][:3]
        status, why_status = "SKIPPED", None
        if T * R < 3 * cost["round_trip_worst"]:
            why_status = f"cost gate: target {T * R:.0f} bp is under 3x the {cost['round_trip_worst']:.1f} bp worst round trip"
        elif exp_net < MIN_EDGE_R:
            why_status = f"model expects {exp_net:+.2f} R after costs (needs {MIN_EDGE_R:+.2f} R)"
        else:
            status = "PICK"
        book = self.books["history" if source == "history" else "live"]
        risk_usd = None
        if status == "PICK":
            block = book.block(f["tc"])
            if not block and live:
                if not q:
                    block = "no live quote"
                elif q[2] > self.limits["max_l1_age_s"]:
                    block = f"best bid/ask is {q[2]:.1f} s old"
                elif spread > self.limits["max_spread_bp"]:
                    block = f"spread {spread:.1f} bp over {self.limits['max_spread_bp']} bp"
                elif any(e - 600 <= f["tc"] <= e + 900 for e in self.events):
                    block = "event blackout"
            if block:
                status, why_status = "BLOCKED", block
            else:
                ok, why_trust = self.trusted(fam)
                status, why_status = ("RECOMMENDED", why_trust) if ok else ("UNPROVEN", "not trusted yet: " + why_trust)
                risk_usd = book.open(f["tc"])
        entry = None                               # None: fills at the next candle's open (replay, cloud, or a stale live quote)
        if live and fresh:
            entry = (q[1] if side > 0 else q[0]) * (1 + side * self.slip / 1e4)
        rec = {"id": rid, "t": int(f["tc"]), "time_utc": utc(f["tc"]), "time_ist": ist(f["tc"]), "source": source, "version": VERSION,
               "symbol": sym, "family": fam, "family_name": p["name"], "hypothesis": p["hypothesis"], "side": "BUY" if side > 0 else "SELL",
               "dir": side, "status": status, "status_why": why_status, "signal_px": f["c"], "entry": entry, "stop_bp": round(R, 2),
               "target_r": T, "time_min": p["time_min"], "regime": f["regime"], "reasoning": c["why"], "invalidation": c["invalidation"],
               "score": round(score, 4), "exp_gross_r": round(exp_gross, 3), "exp_net_r": round(exp_net, 3), "costs_bp": {k: round(v, 2) for k, v in cost.items()},
               "spread_bp": round(spread, 3), "model_n": m.n, "helps": helps, "hurts": hurts,
               "contrib": [[k, round(v, 3)] for k, v in contrib[:6]], "x": {k: round(v, 4) for k, v in x.items()}, "z": z,
               "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items() if k not in ("prev",)},
               "risk_usd": round(risk_usd, 2) if risk_usd else None, "outcome": None,
               "path": {"mfe_bp": 0.0, "mae_bp": 0.0, "bars": 0, "relvol_sum": 0.0, "max_range_atr": 0.0, "t_mfe": None, "first_bp": None}}
        if entry is not None:
            self._levels(rec)
        self.open[rid] = rec
        if source != "history":
            self.recent.append(rec)
        self.out.put(("rec", rec))

    def _levels(self, rec: dict):
        e, s, R = rec["entry"], rec["dir"], rec["stop_bp"]
        rec["stop"] = e * (1 - s * R / 1e4)
        rec["target"] = e * (1 + s * rec["target_r"] * R / 1e4)
        rec["expires_t"] = rec["t"] + rec["time_min"] * 60
        if rec["risk_usd"]:
            qty = rec["risk_usd"] / (e * R / 1e4)
            n = math.floor(qty / CONTRACT[rec["symbol"]] + 1e-9)
            rec["size"] = {"contracts": n, "coins": round(n * CONTRACT[rec["symbol"]], 6), "notional_usd": round(n * CONTRACT[rec["symbol"]] * e, 2),
                           "risk_usd": rec["risk_usd"], "risk_pct": RISK_PCT}

    def _advance(self, rec: dict, bar: list, f: dict | None):
        t, o, h, l, c, v = bar
        s, slip = rec["dir"], self.slip
        if rec["entry"] is None:                     # replay: fill at this candle's open + half the spread + slippage
            rec["entry"] = o * (1 + s * (rec["spread_bp"] / 2 + slip) / 1e4)
            self._levels(rec)
        e, P = rec["entry"], rec["path"]
        fav, adv = s * bp(h if s > 0 else l, e), s * bp(l if s > 0 else h, e)
        P["bars"] += 1
        if P["first_bp"] is None:
            P["first_bp"] = round(s * bp(c, e), 2)
        if fav > P["mfe_bp"]:
            P["mfe_bp"], P["t_mfe"] = fav, P["bars"]
        P["mae_bp"] = min(P["mae_bp"], adv)
        if f:
            P["relvol_sum"] += f["relvol"]
            P["max_range_atr"] = max(P["max_range_atr"], (h - l) / f["atr1"] if f["atr1"] else 0)
        stop_hit = (l <= rec["stop"]) if s > 0 else (h >= rec["stop"])
        tgt_hit = (h > rec["target"]) if s > 0 else (l < rec["target"])
        if stop_hit:                                 # a candle touching both counts as the stop (pessimistic)
            gap = (o <= rec["stop"]) if s > 0 else (o >= rec["stop"])
            px = (o if gap else rec["stop"]) * (1 - s * slip / 1e4)
            return self._resolve(rec, "stop", px, t + 60, f, "taker", (h - l) / f["atr1"] if f and f["atr1"] else None)
        if tgt_hit:
            return self._resolve(rec, "target", rec["target"], t + 60, f, "maker")
        if t + 60 >= rec["expires_t"]:
            return self._resolve(rec, "time", c * (1 - s * (rec["spread_bp"] / 2 + slip) / 1e4), t + 60, f, "taker")
        if rec["source"] != "history":
            rec["mark_r"] = round(s * bp(c, e) / rec["stop_bp"], 3)

    def _resolve(self, rec: dict, outcome: str, px: float, t: int, f: dict | None, exit_kind: str, stop_bar_atr=None):
        del self.open[rec["id"]]
        R, T, s, fam = rec["stop_bp"], rec["target_r"], rec["dir"], rec["family"]
        cost = rec["costs_bp"]
        gross_bp = s * bp(px, rec["entry"])
        cost_bp = cost["entry"] + (cost["exit_target"] if exit_kind == "maker" else cost["exit_taker"])
        gross_r, net_r = gross_bp / R, (gross_bp - cost_bp) / R
        y = max(0.0, min(1.0, (gross_r + 1) / (T + 1)))
        m = self.models[fam]
        pre = rec["score"]
        delta = m.learn(rec["z"], y)
        post = m.score(rec["z"])
        is_pick = rec["status"] in ("RECOMMENDED", "UNPROVEN", "BLOCKED")
        self.all[fam].add(net_r, outcome == "target")
        if is_pick:
            self.picks[fam].add(net_r, outcome == "target")
            if rec["source"] != "history":
                self.live_picks[fam].add(net_r, outcome == "target")
        band = min(9, int(pre * 10))
        cb = self.calib[fam][band]
        cb[0] += 1
        cb[1] += rec["exp_gross_r"]
        cb[2] += gross_r
        if rec["risk_usd"]:
            self.books["history" if rec["source"] == "history" else "live"].close(t, rec["risk_usd"], net_r)
        rec.update(outcome=outcome, exit_px=round(px, 4), exit_t=t, exit_time_utc=utc(t), exit_time_ist=ist(t), held_min=round((t - rec["t"]) / 60),
                   gross_bp=round(gross_bp, 2), cost_bp=round(cost_bp, 2), gross_r=round(gross_r, 3), net_r=round(net_r, 3),
                   pnl_usd=round(rec["risk_usd"] * net_r, 2) if rec["risk_usd"] else None, label=round(y, 3))
        top = sorted(delta.items(), key=lambda kv: -abs(kv[1]))[:3]
        learn = {"t": t, "time_ist": ist(t), "id": rec["id"], "family": fam, "symbol": rec["symbol"], "source": rec["source"], "outcome": outcome,
                 "net_r": rec["net_r"], "score_before": round(pre, 4), "score_after": round(post, 4), "model_n": m.n,
                 "changes": [[k, round(d, 5), round(m.w[k], 4)] for k, d in top]}
        rec["learned"] = learn
        rec["explanation"] = explain(rec, f, stop_bar_atr)
        self.out.put(("rec", rec))
        if rec["source"] != "history":
            self.log.append(learn)
            self.out.put(("log", learn))

    # ---- driving it: replay, catch-up, live ---------------------------------------------------------------------------
    def _process(self, bars_by_sym: dict, source: str, warm: dict | None = None):
        """Candles of both coins in time order. warm = {sym: t}: candles up to t only rebuild the features (after a restart)."""
        warm = warm or {}
        merged = sorted(((b[0], s, b) for s, bs in bars_by_sym.items() for b in bs), key=lambda x: (x[0], x[1]))
        total = max(1, len(merged))
        for i, (t, s, b) in enumerate(merged):
            w = t <= warm.get(s, -1)
            if not w and t <= self.last_t[s]:
                continue
            with self.lock:
                self.on_bar(s, b, source, warm_only=w)
            if i % 2000 == 0:
                self.progress = i / total
        self.progress = 1.0

    def start(self):
        """History replay on first start (or catch-up after a restart), then live, one closed candle at a time."""
        threading.Thread(target=self.writer, daemon=True, name="advisor-writer").start()
        try:
            now = int(time.time()) // 60 * 60
            if not any(self.last_t.values()):
                self.phase = f"learning from {HISTORY_DAYS} days of 1-minute history"
                start = now - HISTORY_DAYS * 86400
                data = {s: self._history(s, start, now) for s in SYMBOLS}
                self._process(data, "history")
            else:
                self.phase = "catching up on candles missed while the app was off"
                self.catch_up("catch-up", now)
            with self.lock:
                self.live_since = self.live_since or int(time.time())
                self.out.put(("state", self._state()))
            self.phase = "live"
        except Exception as e:
            self.phase, self.error = "error", f"{type(e).__name__}: {e}"
            raise
        while True:
            time.sleep(max(1.0, 63 - time.time() % 60))     # 3 s after each minute closes
            try:
                now = int(time.time())
                data = {s: self.fetch(s, self.last_t[s] + 60, now) for s in SYMBOLS}
                if any(data.values()):
                    self._process(data, "live")
                    with self.lock:
                        self.out.put(("state", self._state()))
                self.error = None
            except Exception as e:  # keep going; the next minute retries and fetches whatever was missed
                self.error = f"{utc(time.time())} {type(e).__name__}: {e}"

    def catch_up(self, source: str, now: int | None = None):
        """Process every candle closed since the last one seen (features rebuilt from the candles before it). Used after a
        restart, and by cloud.py, where each scheduled run is one catch-up (source 'cloud': entries fill at the next
        candle's open, as in the replay, because the run is minutes behind the market)."""
        now = now or int(time.time()) // 60 * 60
        warm = dict(self.last_t)
        data = {s: self.fetch(s, warm[s] - (WARM_BARS + 300) * 60, now) for s in SYMBOLS}
        self._process(data, source, warm=warm)
        with self.lock:
            self.out.put(("state", self._state()))

    def _history(self, sym: str, start: int, end: int) -> list:
        cache = self.dir / f"{sym}_1m.json"
        bars = json.loads(cache.read_text()) if cache.exists() else []
        bars = [b for b in bars if b[0] >= start]
        have_lo, have_hi = (bars[0][0], bars[-1][0]) if bars else (end, start - 60)
        if have_lo > start + 3600:
            bars = self.fetch(sym, start, have_lo) + bars
        bars += self.fetch(sym, max(start, (bars[-1][0] + 60) if bars else start), end)
        cache.write_text(json.dumps(bars))
        return bars

    # ---- page -----------------------------------------------------------------------------------------------------------
    def view(self, source: str = "live", limit: int = 150) -> dict:
        with self.lock:
            fams = {}
            for k, p in FAMILIES.items():
                ok, why = self.trusted(k)
                m = self.models[k]
                days = sorted(self.weights_day)
                w0 = self.weights_day[days[-2]][k] if len(days) >= 2 else (self.weights_day[days[-1]][k] if days else m.w)
                fams[k] = {"name": p["name"], "hypothesis": p["hypothesis"], "rule": {kk: p[kk] for kk in ("min_stop_bp", "stop_atr5", "target_r", "time_min")},
                           "trusted": ok, "trust_why": why, "learned": m.n, "all": self.all[k].summary(), "picks": self.picks[k].summary(),
                           "live_picks": self.live_picks[k].summary(),
                           "weights": [[f, FEAT_LABEL[f], round(m.w[f], 4), round(m.w[f] - w0.get(f, 0), 4)] for f in FEATS],
                           "calib": [[i / 10, n, round(a / n, 3) if n else None, round(b / n, 3) if n else None] for i, (n, a, b) in enumerate(self.calib[k])]}
            coins = {}
            for s in SYMBOLS:
                f = self.feat[s].last or {}
                q = self.quote(s) if self.quote else None
                coins[s] = {"price": (q[0] + q[1]) / 2 if q and q[0] and q[1] else f.get("c"), "regime": f.get("regime"), "last_candle": utc(self.last_t[s] + 60) if self.last_t[s] else None,
                            "features": {k: round(f[k], 4) for k in ("atr5_bp", "compression", "relvol", "rv240", "vwap", "ema60", "ema240", "hi60", "lo60", "hi240", "lo240") if k in f}}
            books = {}
            for k, b in self.books.items():
                d = b.d
                books[k] = {kk: d[kk] for kk in ("equity", "peak", "max_dd_pct", "n", "open", "trades_day")} | {"curve": d["curve"][-1500:]}
            strip = lambda r: {k: (dict(v) if isinstance(v, dict) else v) for k, v in r.items() if k != "z"}  # noqa: E731  copies, read outside the lock
            open_now = [strip(r) for r in sorted((r for r in self.open.values() if r["source"] != "history"), key=lambda r: -r["t"])]
            out = {"now": time.time(), "version": VERSION, "versions_tried": VERSIONS_TRIED, "phase": self.phase, "progress": round(self.progress, 3),
                   "error": self.error, "live_since": self.live_since, "min_edge_r": MIN_EDGE_R, "trust": TRUST, "risk_pct": RISK_PCT,
                   "fees_bp": self.fees, "families": fams, "coins": coins, "books": books, "open": open_now,
                   "log": list(self.log)[-60:][::-1]}
            if source == "live":
                out["recs"] = [strip(r) for r in list(self.recent)[-limit:][::-1]]
        if source != "live":
            db = sqlite3.connect(f"file:{self.dbpath}?mode=ro", uri=True)
            out["recs"] = [json.loads(d) for (d,) in db.execute("SELECT data FROM recs WHERE source='history' ORDER BY t DESC LIMIT ?", (limit,))]
            db.close()
        return out

    TAKEN = ("RECOMMENDED", "UNPROVEN")             # the ideas the virtual account took (BLOCKED ones were not taken)
    SLIM = ("id", "t", "symbol", "side", "status", "source", "family_name", "entry", "stop", "target", "stop_bp", "target_r", "time_min",
            "expires_t", "outcome", "exit_px", "exit_t", "net_r", "gross_r", "pnl_usd", "mark_r", "time_ist", "exit_time_ist", "score", "exp_net_r", "reasoning", "status_why", "invalidation")

    def trades(self, sym: str | None = None, lo: int = 0, hi: int = 2 ** 40, shadow: bool = False) -> list:
        """Every idea the account took (history replay and live), newest first, with what the chart needs to draw it.
        Open ideas come from memory (live price); closed ones from the database. shadow=True adds the live candidates the
        model skipped but is still following to their outcome (drawn dashed: not trades, but what is happening now)."""
        db = sqlite3.connect(f"file:{self.dbpath}?mode=ro", uri=True)
        q = f"SELECT data FROM recs WHERE status IN ({','.join('?' * len(self.TAKEN))}) AND t <= ?" + (" AND symbol = ?" if sym else "")
        rows = {(d := json.loads(x))["id"]: d for (x,) in db.execute(q, (*self.TAKEN, hi) + ((sym,) if sym else ()))}
        db.close()
        with self.lock:
            for r in list(self.open.values()) + list(self.recent):
                current_shadow = shadow and r["id"] in self.open and r["source"] != "history"
                if (r["status"] in self.TAKEN or current_shadow) and (not sym or r["symbol"] == sym) and r["t"] <= hi:
                    rows[r["id"]] = r
            out = []
            for r in rows.values():
                if not r.get("entry"):
                    continue
                end = r.get("exit_t") or max(r.get("expires_t") or r["t"], r["t"])
                if end < lo:
                    continue
                d = {k: r.get(k) for k in self.SLIM}
                d["end"] = end
                d["shadow"] = r["status"] not in self.TAKEN
                d["explanation"] = r.get("explanation")
                out.append(d)
        return sorted(out, key=lambda r: -r["t"])

    def bars(self, sym: str, start: int | None = None, end: int | None = None) -> dict:
        """The latest 12 hours from memory, or (start, end) fetched from Delta for looking at an older trade, plus the trades
        that overlap the window."""
        if start and end:
            end = min(end, int(time.time()))
            start = max(start, end - 3990 * 60)
            key = (sym, start // 60, end // 60)
            if key not in self._bar_cache:
                if len(self._bar_cache) > 50:
                    self._bar_cache.clear()
                self._bar_cache[key] = self.fetch(sym, start, end)
            bs = self._bar_cache[key]
        else:
            with self.lock:
                bs = list(self.feat[sym].bars)
        lo, hi = (bs[0][0], bs[-1][0] + 60) if bs else (0, 0)
        return {"bars": [{"time": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in bs],
                "trades": self.trades(sym, lo, hi, shadow=True) if bs else []}

def explain(rec: dict, f: dict | None, stop_bar_atr=None) -> dict:
    """Why it worked or failed, in plain sentences, from the path, the context change and the costs. Facts only."""
    R, T, P = rec["stop_bp"], rec["target_r"], rec["path"]
    mfe, mae = P["mfe_bp"] / R, P["mae_bp"] / R
    out, why = rec["outcome"], []
    if out == "target":
        head = f"Worked: reached the target (+{T:g} R, {T * R:.0f} bp) in {rec['held_min']} min."
        why.append("It went the right way almost at once." if mae > -0.3 else
                   f"It first went against the idea to {mae:+.1f} R, then recovered; the stop gave it enough room.")
    elif out == "stop":
        if mfe >= 1.0:
            head = f"Failed after working: it was up {mfe:+.1f} R ({mfe / T * 100:.0f}% of the way to the target), then reversed to the stop."
        elif mfe < 0.3:
            head = f"Failed: wrong from the start; the best it reached was {mfe:+.1f} R before the stop."
        else:
            head = f"Failed: some follow-through ({mfe:+.1f} R) faded into the stop."
        if stop_bar_atr and stop_bar_atr >= 3:
            why.append(f"The stop was hit by one wide candle ({stop_bar_atr:.1f}x a normal 1-minute range): a volatility spike.")
        if P["bars"] <= 3:
            why.append(f"It was stopped within {P['bars']} minute(s) of entry.")
    else:
        head = f"No follow-through: neither the target nor the stop in {rec['time_min']} min; closed at {rec['gross_r']:+.2f} R before costs."
        why.append(f"Best point {mfe:+.1f} R, worst {mae:+.1f} R: the market went sideways after the signal.")
    if f:
        if f["regime"] != rec["regime"]:
            why.append(f"The market changed from '{rec['regime']}' to '{f['regime']}' during the idea.")
        if (f["c"] > f["ema240"]) != (rec["features"]["c"] > rec["features"]["ema240"]):
            why.append("The 4-hour trend flipped during the idea.")
        avg = P["relvol_sum"] / P["bars"] if P["bars"] else 0
        if avg and avg < 0.8:
            why.append(f"Volume dried up after the signal ({avg:.1f}x normal on average): nobody followed.")
        elif avg > 1.5:
            why.append(f"Volume stayed heavy ({avg:.1f}x normal on average).")
    model = f"Before entry the model scored it {rec['score'] * 100:.0f}% (expected {rec['exp_net_r']:+.2f} R after costs)"
    if rec["helps"] or rec["hurts"]:
        model += ": " + "; ".join(x for x in (("helped by " + ", ".join(rec["helps"])) if rec["helps"] else "",
                                               ("held back by " + ", ".join(rec["hurts"])) if rec["hurts"] else "") if x)
    picked = rec["status"] in ("RECOMMENDED", "UNPROVEN", "BLOCKED")
    agreed = (rec["net_r"] > 0) == picked
    model += ". " + ("That matched the result." if agreed else "The result went the other way.")
    L = rec.get("learned") or {}
    changes = ", ".join(f"{FEAT_LABEL[k]} {d:+.3f}" for k, d, _ in L.get("changes", []))
    learned = (f"Learned: the {rec['family_name']} model's score for this exact situation moved from {L.get('score_before', 0) * 100:.0f}% to "
               f"{L.get('score_after', 0) * 100:.0f}% (biggest weight changes: {changes}). Rules, stops, targets and limits did not change.")
    costs = f"Costs {rec['cost_bp']:.1f} bp = {rec['cost_bp'] / R:.2f} R: {rec['gross_r']:+.2f} R before costs, {rec['net_r']:+.2f} R after."
    return {"headline": head, "why": why, "model": model, "costs": costs, "learned": learned}
