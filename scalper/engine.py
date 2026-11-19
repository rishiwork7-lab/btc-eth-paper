"""The fast path: every live event updates the market, checks fills and exits, and on each one-minute close evaluates the
setups through the cost gate and the risk limits. PAPER only. Deterministic code; no AI model in this loop (skill §3).

Paper fills follow the skill's §11 strictly: an aggressive order fills at the opposite best quote after the modelled latency
(one tick worse if larger than the shown size); a resting take-profit fills only when a trade prints strictly through it or
the opposite quote crosses it; a stop fills at the first trade or quote at or beyond it, minus slippage. Every fill pays
Delta's fee plus 18% GST.

Lessons from the deleted crypto desk, built in: only the writer thread touches the database; a state snapshot is serialized
the moment it is taken; order and signal numbers continue past the highest recorded one after a restart."""
import json
import math
import queue
import random
import sqlite3
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .market import Instrument, bp
from .setups import SETUPS, evaluate


def utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ist(t: float) -> str:
    return datetime.fromtimestamp(t + 19800, timezone.utc).strftime("%H:%M:%S IST")


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, t REAL, symbol TEXT, side TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS notrade (t REAL, symbol TEXT, why TEXT, PRIMARY KEY (t, symbol));
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, t REAL, data TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, setup TEXT, symbol TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity (t INTEGER PRIMARY KEY, equity REAL);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS opt_trades (id INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS opt_events (t REAL, kind TEXT, data TEXT);
"""


class Engine:
    def __init__(self, root: Path, products: dict, conf: dict | None = None):
        self.root = Path(root)
        self.conf = conf or tomllib.loads((self.root / "config.toml").read_text())
        # False (set by run.py since 2026-11-11, at the owner's request): scalping is removed; the engine only keeps live
        # prices for the strategy books and owns the database writer. No signals, orders or scalper journal rows.
        self.scalping = True
        self.L, self.C, self.G = self.conf["limits"], self.conf["costs"], self.conf["gate"]
        self.fees = {s: {"maker": p["maker"] * (1 + self.C["gst"]) * 1e4, "taker": p["taker"] * (1 + self.C["gst"]) * 1e4} for s, p in products.items()}
        self.inst = {s: Instrument(s, p["index"], p["contract"], p["tick"]) for s, p in products.items()}
        self.by_index = {i.index_sym: i for i in self.inst.values()}
        (self.root / "data").mkdir(exist_ok=True)
        self.dbpath = self.root / "data" / "scalper.sqlite"
        db = sqlite3.connect(str(self.dbpath))
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.commit()
        cap = float(self.conf["capital"])
        self.acct = {"cash": cap, "capital": cap, "peak": cap, "day": None, "day_start": cap, "week": None, "week_start": cap,
                     "halted": None, "pause_until": 0.0, "consec": 0, "consec_day": 0, "trades_day": 0, "fees_paid": 0.0}
        self.pos: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self.seq = {"order": 0, "signal": 0}
        self.started, self.events = time.time(), 0
        self.recent = []              # latest signals (BUY/SELL and gate/risk rejections), newest last
        self.last_no_trade: dict = {}
        self.stats: dict[str, list] = {k: [] for k in SETUPS}   # per setup: (hit_tp1, net_bp, confidence)
        self.out: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.minute = int(time.time() // 60)
        self._restore(db)
        db.close()

    # ---- persistence: restore at start, then the writer thread is the only writer ----------------------------------------
    def _restore(self, db):
        r = db.execute("SELECT v FROM state WHERE k='engine'").fetchone()
        if r:
            s = json.loads(r[0])
            self.acct.update(s["acct"])
            self.pos = s["pos"]               # resting stop/target orders are kept: they live on the exchange in a live setup
            self.seq.update(s["seq"])
        for kind, table, prefix in (("order", "orders", "O-"), ("signal", "signals", "S-")):
            ids = [row[0] for row in db.execute(f"SELECT id FROM {table}")]
            n = max((int(i.rsplit("-", 1)[1]) for i in ids if i.rsplit("-", 1)[1].isdigit()), default=0)
            self.seq[kind] = max(self.seq[kind], n)
        for setup, data in db.execute("SELECT setup, data FROM trades ORDER BY id"):
            d = json.loads(data)
            self.stats.setdefault(setup, []).append((d["hit_tp1"], d["net_bp"], d["confidence"]))

    def snapshot_state(self):
        """Frozen now (serialized here), not later in the writer, so cash, positions and counters always agree."""
        self.out.put(("state", json.dumps({"acct": self.acct, "pos": self.pos, "seq": self.seq}, default=str)))

    def writer(self):
        db = sqlite3.connect(str(self.dbpath), timeout=10)
        while True:
            kind, x = self.out.get()
            try:
                self._write(db, kind, x)
            except Exception as e:  # reported, never silent
                with open(self.root / "data" / "errors.log", "a") as f:
                    f.write(f"{utc(time.time())} WRITE FAILED ({kind}): {type(e).__name__}: {e}\n")

    def flush(self):
        """Batch mode (cloud.py): write everything queued now, on this thread, instead of the writer thread."""
        db = sqlite3.connect(str(self.dbpath), timeout=10)
        while not self.out.empty():
            kind, x = self.out.get()
            self._write(db, kind, x)
        db.close()

    def _write(self, db, kind, x):
        if kind == "signal":
            db.execute("INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?)", (x["id"], x["t"], x["instrument"], x["side"], json.dumps(x, default=str)))
        elif kind == "notrade":
            db.execute("INSERT OR REPLACE INTO notrade VALUES (?,?,?)", x)
        elif kind == "order":
            db.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?)", (x["id"], x["t"], json.dumps(x, default=str)))
        elif kind == "trade":
            db.execute("INSERT INTO trades (setup, symbol, data) VALUES (?,?,?)", (x["setup"], x["symbol"], json.dumps(x, default=str)))
        elif kind == "equity":
            db.execute("INSERT OR REPLACE INTO equity VALUES (?,?)", x)
        elif kind == "state":
            db.execute("INSERT OR REPLACE INTO state VALUES ('engine', ?)", (x,))
        elif kind == "opt_state":
            db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", x)
        elif kind == "opt_trade":
            db.execute("INSERT INTO opt_trades (data) VALUES (?)", (json.dumps(x, default=str),))
        elif kind == "opt_event":
            db.execute("INSERT INTO opt_events VALUES (?,?,?)", (x["t"], x["kind"], json.dumps(x, default=str)))
        db.commit()

    # ---- events: the fast path --------------------------------------------------------------------------------------------
    def on_message(self, m: dict, rx: float | None = None):
        rx = rx or time.time()
        typ, sym = m.get("type"), m.get("symbol")
        with self.lock:
            self.events += 1
            closed = None
            if typ == "l1_orderbook" and sym in self.inst:
                i = self.inst[sym]
                i.on_l1(m, rx)
                closed = i.bar_update(rx, i.mid)
                if self.scalping:
                    self._fill_pending(i, rx)
                    self._exits(i, rx, None)
            elif typ == "all_trades" and sym in self.inst:
                i = self.inst[sym]
                t = i.on_trade(m, rx)
                closed = i.bar_update(rx, t[1], t[2], t[3])
                if self.scalping:
                    self._fill_pending(i, rx)
                    self._exits(i, rx, t)
            elif typ == "v2/ticker" and sym in self.inst:
                i = self.inst[sym]
                i.mark = float(m["mark_price"]) if m.get("mark_price") else i.mark
                i.funding = float(m["funding_rate"]) if m.get("funding_rate") not in (None, "") else i.funding
                i.oi = m.get("oi_value") or i.oi
            elif typ == "v2/spot_price" and m.get("s") in self.by_index:
                i = self.by_index[m["s"]]
                i.index, i.index_rx = float(m["p"]), rx
            if not self.scalping:
                return
            if closed:
                self._on_bar(self.inst[sym], rx)
            minute = int(rx // 60)
            if minute != self.minute:
                self.minute = minute
                self._roll_day(rx)
                self.out.put(("equity", (minute * 60, round(self.equity(), 4))))
                self.snapshot_state()

    # ---- account ------------------------------------------------------------------------------------------------------
    def unrealized(self, p: dict) -> float:
        i = self.inst[p["symbol"]]
        px = (i.bid if p["side"] > 0 else i.ask) or p["entry"]
        return p["side"] * p["qty"] * (px - p["entry"])

    def equity(self) -> float:
        return self.acct["cash"] + sum(self.unrealized(p) for p in self.pos.values())

    def _roll_day(self, rx):
        a, eq = self.acct, self.equity()
        day, week = utc(rx)[:10], datetime.fromtimestamp(rx, timezone.utc).strftime("%G-W%V")
        if a["day"] != day:
            a.update(day=day, day_start=eq, consec_day=0, trades_day=0)
            if a["halted"] and a["halted"].startswith("daily"):
                a["halted"] = None
        if a["week"] != week:
            a.update(week=week, week_start=eq)
        a["peak"] = max(a["peak"], eq)
        if not a["halted"]:
            if eq <= a["peak"] * (1 - self.L["max_drawdown_pct"] / 100):
                a["halted"] = f"drawdown {(1 - eq / a['peak']) * 100:.1f}% from the peak (limit {self.L['max_drawdown_pct']}%): halted until the owner resets"
            elif eq <= a["week_start"] * (1 - self.L["weekly_loss_pct"] / 100):
                a["halted"] = f"weekly loss {(1 - eq / a['week_start']) * 100:.1f}% (limit {self.L['weekly_loss_pct']}%): owner review needed"
            elif eq <= a["day_start"] * (1 - self.L["daily_loss_pct"] / 100):
                a["halted"] = f"daily loss {(1 - eq / a['day_start']) * 100:.1f}% (limit {self.L['daily_loss_pct']}%) until 00:00 UTC"

    # ---- decisions on each one-minute close ----------------------------------------------------------------------------
    def _on_bar(self, i: Instrument, rx: float):
        f = i.compute()
        cands, notes = evaluate(i.symbol, list(i.bars), f)
        if not cands:
            why = notes[0] if notes else "no setup"
            self.last_no_trade[i.symbol] = {"t": rx, "time": utc(rx), "why": why, "all": notes}
            self.out.put(("notrade", (int(rx // 60 * 60), i.symbol, " | ".join(notes))))
            return
        for c in cands:
            sig = self._signal(i, c, f, rx)
            self.recent = (self.recent + [sig])[-200:]
            self.out.put(("signal", dict(sig)))
            if sig["side"] in ("BUY", "SELL"):
                self._place_entry(i, sig, rx)
                break

    def setup_estimate(self, setup: str, W: float, R: float) -> tuple[float, int]:
        """Probability of reaching TP1 before the stop: the setup's own record, shrunk toward the no-edge value (skill §4/§7).
        With no record the estimate is exactly no edge before costs, so a new setup can never look qualified by assumption."""
        recs = self.stats.get(setup, [])
        p0, w = R / (W + R), 20
        hits = sum(1 for h, _, _ in recs if h)
        return (hits + p0 * w) / (len(recs) + w), len(recs)

    def _signal(self, i: Instrument, c: dict, f: dict, rx: float) -> dict:
        self.seq["signal"] += 1
        sid = f"S-{utc(rx)[:10]}-{self.seq['signal']:05d}"
        s = SETUPS[c["setup"]]
        side, ref, R = c["side"], i.mid or c["ref"], c["stop_bp"]
        T1, T2 = s["tp_r"][0] * R, s["tp_r"][1] * R
        rnd = lambda x: round(round(x / i.tick) * i.tick, 6)  # noqa: E731
        stop = rnd(ref * (1 - side * R / 1e4))
        tps = [rnd(ref * (1 + side * T1 / 1e4)), rnd(ref * (1 + side * T2 / 1e4))]
        spread = i.spread_bp() or 0.0
        fee = self.fees[i.symbol]
        funding_bp = max(0.0, side * (i.funding or 0.0) * 100 * s["time_stop_min"] / 480)   # % per 8h -> bp over the hold
        cost = {"entry_fee": fee["taker"], "exit_fee_worst": fee["taker"], "spread": spread, "slippage": 2 * self.C["slippage_bp"], "funding": round(funding_bp, 3)}
        rt = sum(cost.values())
        W = 0.5 * T1 + 0.25 * T2       # TP1 on half; the other half reaches TP2 about half the time, else exits at entry
        p, n = self.setup_estimate(c["setup"], W, R)
        exp_gross = p * W - (1 - p) * R
        target_avg = 0.5 * T1 + 0.5 * T2
        qualified = n >= self.G["qualified_min_trades"] and exp_gross >= self.G["min_expected_cost_ratio"] * rt
        status = "qualified" if qualified else "exploring"
        sig = {"id": sid, "t": rx, "time_utc": utc(rx), "time_ist": ist(rx), "mode": self.conf["mode"], "instrument": i.symbol,
               "setup": c["setup"], "hypothesis": s["hypothesis"], "regime": f.get("regime"), "status": status,
               "entry": {"type": "market (taker)", "price": rnd(ref), "valid_until_utc": utc(rx + 60)},
               "stop_loss": stop, "take_profit": [{"price": tps[0], "size_pct": 50}, {"price": tps[1], "size_pct": 50}],
               "time_stop_min": s["time_stop_min"], "trail": "after TP1, stop moves to entry",
               "stop_bp": round(R, 1), "risk_reward": round(target_avg / R, 2),
               "costs_bp": {**{k: round(v, 2) for k, v in cost.items()}, "round_trip": round(rt, 2), "target_cost_ratio": round(target_avg / rt, 2)},
               "expected_gross_bp": round(exp_gross, 2), "expected_net_bp": round(exp_gross - rt, 2),
               "confidence": round(p, 3), "confidence_basis": f"{c['setup']} record n={n}" + (" (none yet: set to no edge)" if n == 0 else ""),
               "reasoning": c["reasoning"], "invalidation": c["invalidation"], "features": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()}}
        why = None
        if target_avg < self.G["min_target_cost_ratio"] * rt:
            why = f"cost gate: average target {target_avg:.1f} bp is under {self.G['min_target_cost_ratio']:g}x the {rt:.1f} bp round-trip cost"
        else:
            why = self._risk_block(i, rx)
        size = None if why else self._size(i, ref, R, status)
        if not why and (not size or size["contracts"] < 1):
            why = "size below one contract after the risk caps"
        if why:
            sig.update(side="NO_TRADE", wanted="BUY" if side > 0 else "SELL", why=why)
        else:
            sig.update(side="BUY" if side > 0 else "SELL", size=size)
        return sig

    def _risk_block(self, i: Instrument, rx: float) -> str | None:
        a, L = self.acct, self.L
        if a["halted"]:
            return a["halted"]
        if rx < a["pause_until"]:
            return f"paused after {L['consecutive_losses_pause']} losses in a row until {utc(a['pause_until'])} UTC"
        if a["consec_day"] >= L["consecutive_losses_day"]:
            return f"{L['consecutive_losses_day']} losses today: done for the UTC day"
        if a["trades_day"] >= L["max_trades_per_day"]:
            return f"{L['max_trades_per_day']} trades today"
        if i.symbol in self.pos or i.symbol in self.pending:
            return f"already in a {i.symbol} position"
        if len(self.pos) + len(self.pending) >= L["max_positions"]:
            return f"{L['max_positions']} positions open"
        if not i.l1_rx or rx - i.l1_rx > L["max_l1_age_s"]:
            return f"best bid/ask is {rx - i.l1_rx:.1f} s old (limit {L['max_l1_age_s']} s)"
        if not i.index_rx or rx - i.index_rx > L["max_index_age_s"]:
            return f"index is stale (limit {L['max_index_age_s']} s)"
        if (i.spread_bp() or 99) > L["max_spread_bp"]:
            return f"spread {i.spread_bp():.1f} bp over {L['max_spread_bp']} bp"
        for e in self.conf.get("events", {}).get("times", []):
            et = datetime.fromisoformat(e.replace("Z", "+00:00")).timestamp()
            if et - 600 <= rx <= et + 900:
                return f"event blackout around {e}"
        return None

    def _size(self, i: Instrument, ref: float, R: float, status: str) -> dict | None:
        eq, a, L = self.equity(), self.acct, self.L
        risk_pct = L["risk_per_trade_pct"]
        notes = []
        if status != "qualified":
            risk_pct /= 2
            notes.append("half size: setup still exploring")
        if eq < a["peak"] * (1 - L["half_size_drawdown_pct"] / 100):
            risk_pct /= 2
            notes.append("half size: drawdown over 4%")
        risk_usd = eq * risk_pct / 100
        qty = risk_usd / (ref * R / 1e4)
        open_notional = sum(p["qty"] * p["entry"] for p in self.pos.values())
        qty = min(qty, max(0.0, (L["max_leverage"] * eq - open_notional) / ref))
        open_risk = sum(p["qty"] * abs(p["entry"] - p["stop"]) for p in self.pos.values())
        qty = min(qty, max(0.0, (L["open_risk_pct"] / 100 * eq - open_risk) / (ref * R / 1e4)))
        contracts = math.floor(qty / i.contract + 1e-9)
        q = contracts * i.contract
        return {"contracts": contracts, "qty": round(q, 6), "notional_usd": round(q * ref, 2), "risk_usd": round(q * ref * R / 1e4, 2),
                "risk_pct_equity": round(q * ref * R / 1e4 / eq * 100, 3), "leverage": round(q * ref / eq, 2), "notes": notes}

    # ---- paper orders and fills --------------------------------------------------------------------------------------
    def _oid(self) -> str:
        self.seq["order"] += 1
        return f"O-{self.seq['order']:06d}"

    def _place_entry(self, i: Instrument, sig: dict, rx: float):
        side = 1 if sig["side"] == "BUY" else -1
        o = {"id": self._oid(), "t": rx, "time": utc(rx), "symbol": i.symbol, "side": side, "qty": sig["size"]["qty"], "kind": "entry",
             "type": "market", "ready": rx + self.C["latency_s"], "signal": sig, "status": "pending"}
        self.pending[i.symbol] = o
        self.out.put(("order", {k: v for k, v in o.items() if k != "signal"} | {"signal_id": sig["id"]}))

    def _fill_pending(self, i: Instrument, rx: float):
        o = self.pending.get(i.symbol)
        if not o or rx < o["ready"] or not i.bid or not i.ask:
            return
        del self.pending[i.symbol]
        sig, side = o["signal"], o["side"]
        px = i.ask if side > 0 else i.bid
        shown = (i.aq if side > 0 else i.bq) * i.contract
        if o["qty"] > shown > 0:
            px += side * i.tick             # bigger than the shown size: one tick worse (stated assumption)
        px *= 1 + side * self.C["slippage_bp"] / 1e4
        slip = side * bp(px, sig["entry"]["price"])
        limit = max(self.L["max_entry_slip_bp"], 0.25 * sig["stop_bp"])
        if slip > limit:
            o.update(status="cancelled", why=f"would fill {slip:.1f} bp from the signal price (limit {limit:.1f} bp)")
            self.out.put(("order", {k: v for k, v in o.items() if k != "signal"} | {"signal_id": sig["id"]}))
            sig["outcome"] = o["why"]
            self.out.put(("signal", dict(sig)))
            return
        fee = o["qty"] * px * self.fees[i.symbol]["taker"] / 1e4
        self.acct["cash"] -= fee
        self.acct["fees_paid"] += fee
        self.acct["trades_day"] += 1
        stop, tp1, tp2 = sig["stop_loss"], sig["take_profit"][0]["price"], sig["take_profit"][1]["price"]
        self.pos[i.symbol] = {"id": o["id"], "symbol": i.symbol, "side": side, "qty": o["qty"], "qty0": o["qty"], "entry": px, "entry_t": rx,
                              "entry_time": utc(rx), "stop": stop, "stop0": stop, "tp1": tp1, "tp2": tp2, "tp1_done": False,
                              "time_stop_at": rx + sig["time_stop_min"] * 60, "fees": fee, "realized": 0.0, "exits": [],
                              "signal_id": sig["id"], "setup": sig["setup"], "confidence": sig["confidence"], "expected_net_bp": sig["expected_net_bp"],
                              "status": sig["status"], "regime": sig["regime"], "mfe_bp": 0.0, "mae_bp": 0.0, "entry_slip_bp": round(slip, 2),
                              "reasoning": sig["reasoning"]}
        o.update(status="filled", fill_px=px, fill_time=utc(rx), fee=fee)
        self.out.put(("order", {k: v for k, v in o.items() if k != "signal"} | {"signal_id": sig["id"]}))
        sig["outcome"] = f"filled at {px:,.2f}"
        self.out.put(("signal", dict(sig)))

    def _exit_fill(self, p: dict, qty: float, px: float, kind: str, rx: float, why: str):
        fee = qty * px * self.fees[p["symbol"]][kind] / 1e4
        pnl = p["side"] * qty * (px - p["entry"])
        self.acct["cash"] += pnl - fee
        self.acct["fees_paid"] += fee
        p["fees"] += fee
        p["realized"] += pnl
        p["qty"] = round(p["qty"] - qty, 9)
        p["exits"].append({"time": utc(rx), "px": px, "qty": qty, "kind": kind, "why": why, "fee": round(fee, 4)})

    def _exits(self, i: Instrument, rx: float, trade):
        p = self.pos.get(i.symbol)
        if not p:
            return
        s = p["side"]
        if i.mid:
            mv = s * bp(i.mid, p["entry"])
            p["mfe_bp"], p["mae_bp"] = max(p["mfe_bp"], mv), min(p["mae_bp"], mv)
        slip = self.C["slippage_bp"] / 1e4
        tpx = trade[1] if trade else None
        through = lambda lvl: (tpx is not None and s * (tpx - lvl) > 0) or ((i.bid if s > 0 else i.ask) and s * ((i.bid if s > 0 else i.ask) - lvl) > 0)  # noqa: E731
        # stop: first trade or quote at or beyond it; fills there, minus slippage (gaps fill at the gap price)
        hit_px = None
        if tpx is not None and s * (tpx - p["stop"]) <= 0:
            hit_px = tpx
        elif (i.bid if s > 0 else i.ask) and s * ((i.bid if s > 0 else i.ask) - p["stop"]) <= 0:
            hit_px = i.bid if s > 0 else i.ask
        if hit_px is not None:
            why = "stop at entry after TP1" if p["tp1_done"] else "stop-loss"
            self._exit_fill(p, p["qty"], hit_px * (1 - s * slip), "taker", rx, why)
            return self._close(p, rx, why)
        if not p["tp1_done"] and through(p["tp1"]):
            half = round(math.floor(p["qty0"] / 2 / i.contract) * i.contract, 9)
            if half > 0:
                self._exit_fill(p, half, p["tp1"], "maker", rx, "take-profit 1")
            p["tp1_done"] = True
            p["stop"] = p["entry"]
            if p["qty"] <= 1e-12:
                return self._close(p, rx, "take-profit 1")
        if p["tp1_done"] and through(p["tp2"]):
            self._exit_fill(p, p["qty"], p["tp2"], "maker", rx, "take-profit 2")
            return self._close(p, rx, "take-profit 2")
        if rx >= p["time_stop_at"] and i.bid and i.ask:
            px = (i.bid if s > 0 else i.ask) * (1 - s * slip)
            self._exit_fill(p, p["qty"], px, "taker", rx, "time stop")
            return self._close(p, rx, "time stop")

    def _close(self, p: dict, rx: float, why: str):
        del self.pos[p["symbol"]]
        notional = p["qty0"] * p["entry"]
        net = p["realized"] - p["fees"]
        risk = p["qty0"] * abs(p["entry"] - p["stop0"])
        rec = {"symbol": p["symbol"], "setup": p["setup"], "side": "long" if p["side"] > 0 else "short", "qty": p["qty0"], "entry": p["entry"],
               "entry_time": p["entry_time"], "exit_time": utc(rx), "held_min": round((rx - p["entry_t"]) / 60, 1), "exits": p["exits"],
               "exit_reason": why, "gross": round(p["realized"], 4), "fees": round(p["fees"], 4), "net": round(net, 4),
               "net_bp": round(net / notional * 1e4, 2), "r_multiple": round(net / risk, 2) if risk else None, "hit_tp1": p["tp1_done"],
               "mfe_bp": round(p["mfe_bp"], 1), "mae_bp": round(p["mae_bp"], 1), "confidence": p["confidence"], "expected_net_bp": p["expected_net_bp"],
               "status": p["status"], "regime": p["regime"], "signal_id": p["signal_id"], "entry_slip_bp": p["entry_slip_bp"], "reasoning": p["reasoning"]}
        a = self.acct
        if net < 0:
            a["consec"] += 1
            a["consec_day"] += 1
            if a["consec"] >= self.L["consecutive_losses_pause"]:
                a["pause_until"] = rx + self.L["pause_minutes"] * 60
                a["consec"] = 0
        else:
            a["consec"] = 0
        self.stats.setdefault(p["setup"], []).append((rec["hit_tp1"], rec["net_bp"], rec["confidence"]))
        self.out.put(("trade", rec))
        self._roll_day(rx)
        self.snapshot_state()

    # ---- dashboard view ------------------------------------------------------------------------------------------------
    def view(self) -> dict:
        with self.lock:
            now = time.time()
            eq = self.equity()
            a = dict(self.acct)
            insts = {}
            for s, i in self.inst.items():
                f = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in i.feats.items()}
                insts[s] = {"bid": i.bid, "ask": i.ask, "mid": i.mid, "spread_bp": round(i.spread_bp(), 3) if i.spread_bp() is not None else None,
                            "index": i.index, "mark": i.mark, "funding": i.funding, "l1_age": round(now - i.l1_rx, 2) if i.l1_rx else None,
                            "latency_ms": i.latency_ms(), "features": f, "bars": [[b["t"], b["c"]] for b in list(i.bars)[-180:]],
                            "last_no_trade": self.last_no_trade.get(s)}
            pos = []
            for p in self.pos.values():
                u = self.unrealized(p)
                pos.append({k: v for k, v in p.items() if k != "exits"} | {"unrealized": round(u, 4), "exits": p["exits"],
                                                                           "time_left_min": round((p["time_stop_at"] - now) / 60, 1)})
            setups = {}
            for k, s in SETUPS.items():
                recs = self.stats.get(k, [])
                nets = [x[1] for x in recs]
                ci = None
                if len(nets) >= 10:
                    rng = random.Random(7)
                    means = sorted(sum(rng.choice(nets) for _ in nets) / len(nets) for _ in range(1000))
                    ci = [round(means[25], 2), round(means[974], 2)]
                setups[k] = {"hypothesis": s["hypothesis"], "trades": len(recs), "tp1_rate": round(sum(1 for x in recs if x[0]) / len(recs) * 100, 1) if recs else None,
                             "avg_confidence": round(sum(x[2] for x in recs) / len(recs), 3) if recs else None,
                             "avg_net_bp": round(sum(nets) / len(nets), 2) if nets else None, "ci95": ci,
                             "status": "exploring" if len(recs) < self.G["qualified_min_trades"] else "evaluate",
                             "params": {kk: v for kk, v in s.items() if kk != "hypothesis"}}
            return {"now": now, "mode": self.conf["mode"], "started": self.started, "events": self.events,
                    "account": a | {"equity": round(eq, 4), "unrealized": round(eq - a["cash"], 4), "drawdown_pct": round((1 - eq / a["peak"]) * 100, 3),
                                    "day_pnl": round(eq - a["day_start"], 4), "week_pnl": round(eq - a["week_start"], 4)},
                    "limits": self.L, "gate": self.G, "fees_bp": self.fees, "instruments": insts, "positions": pos,
                    "pending": [{k: v for k, v in o.items() if k != "signal"} for o in self.pending.values()],
                    "signals": self.recent[-60:][::-1], "setups": setups}
