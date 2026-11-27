"""The strategy pages: a small read-only web server. Live state comes from the books in memory; trades from the database
(read-only connection, so it never competes with the writer). The scalper dashboard was removed on 2026-11-11."""
import json
import re
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPT_HTML = Path(__file__).with_name("options.html")
ADV_HTML = Path(__file__).with_name("advisor.html")
BRK_HTML = Path(__file__).with_name("breakout.html")
DEP_HTML = Path(__file__).with_name("deploy.html")
DEP_JSON = Path(__file__).with_name("deploy.json")   # build_deploy.py
SIG_HTML = Path(__file__).with_name("signals.html")
SIG_JS = Path(__file__).with_name("signals_core.js")
SIM_HTML = Path(__file__).with_name("simple.html")


CANDLE_CACHE: dict = {}


def options_payload(engine, books) -> dict:
    """The strategy books' live state, their trades (read-only connection) and their backtests: /api/options and the static site."""
    db = sqlite3.connect(f"file:{engine.dbpath}?mode=ro", uri=True)
    trades = [json.loads(d) for (d,) in db.execute("SELECT data FROM opt_trades ORDER BY id DESC")]
    db.close()
    out = []
    for b in books or []:
        bt = engine.root / "backtest" / f"options_backtest{'' if b.book == 'fri-night' else '_' + b.book}.json"
        out.append({"live": b.view(), "trades": [t for t in trades if t.get("book", "fri-night") == b.book],
                    "backtest": json.loads(bt.read_text()) if bt.exists() else None})
    pf = engine.root / "backtest" / "be_backtest.json"
    port = None
    if pf.exists():
        r3 = json.loads(pf.read_text())
        port = {k: r3.get(k) for k in ("blend", "rows", "components", "weights", "leverage", "coins", "capital", "real_13_months", "real_rows")}
    return {"books": out, "portfolio": port}


def serve(engine, port: int, books=None, advisor=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/candles"):
                return self._candles()
            if self.path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/options")
                self.end_headers()
                return
            if self.path in ("/options", "/options/") or self.path.startswith(("/strategy/", "/coin/")):
                return self._send(200, OPT_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path in ("/signals", "/signals/"):
                return self._send(200, SIG_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path in ("/simple", "/simple/", "/simple/index.html"):
                return self._send(200, SIM_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path in ("/signals_core.js", "/simple/signals_core.js"):
                return self._send(200, SIG_JS.read_bytes(), "text/javascript; charset=utf-8")
            if self.path in ("/static.js", "/simple/static.js"):
                stat_js = engine.root / "static.js"
                return self._send(200, stat_js.read_bytes(), "text/javascript; charset=utf-8") if stat_js.exists() else self._send(404, b'// not found', "text/javascript")
            if self.path in ("/deploy", "/deploy/"):
                return self._send(200, DEP_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path.startswith("/api/deploy"):
                return self._send(200, DEP_JSON.read_bytes(), "application/json") if DEP_JSON.exists() else self._send(404, b'{"error": "run build_deploy.py"}', "application/json")
            if self.path in ("/breakout", "/breakout/"):
                return self._send(200, BRK_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path.startswith("/api/breakout"):
                f = engine.root / "backtest" / "breakout.json"
                return self._send(200, f.read_bytes(), "application/json") if f.exists() else self._send(404, b'{"error": "run breakout_backtest.py"}', "application/json")
            if self.path in ("/advisor", "/advisor/"):
                return self._send(200, ADV_HTML.read_bytes(), "text/html; charset=utf-8")
            if self.path.startswith("/api/advisor"):
                if not advisor:
                    return self._send(404, b'{"error": "advisor not running"}', "application/json")
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                try:
                    sym = q.get("symbol") if q.get("symbol") in ("BTCUSD", "ETHUSD") else None
                    if self.path.startswith("/api/advisor/bars"):
                        try:
                            start, end = int(q.get("start", 0)), int(q.get("end", 0))
                        except ValueError:
                            start = end = 0
                        body = advisor.bars(sym or "BTCUSD", start if 0 < start < end else None, end if 0 < start < end else None)
                    elif self.path.startswith("/api/advisor/trades"):
                        body = advisor.trades(sym)
                    else:
                        body = advisor.view("history" if q.get("source") == "history" else "live")
                    return self._send(200, json.dumps(body, default=str).encode(), "application/json")
                except Exception as e:
                    return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
            if self.path.startswith("/api/options"):
                try:
                    return self._send(200, json.dumps(options_payload(engine, books), default=str).encode(), "application/json")
                except Exception as e:
                    return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

    def _candles(self):
        """Read-only proxy for Delta's public candles (the page cannot call Delta directly). Cached for 30 s."""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        sym, res = q.get("symbol", ""), q.get("resolution", "")
        try:
            start, end = int(q.get("start", 0)), int(q.get("end", 0))
        except ValueError:
            start = end = 0
        if not re.fullmatch(r"[A-Z0-9.:_-]{2,40}", sym) or res not in ("5m", "15m", "1h", "4h", "1d") or not 0 < start < end or end - start > 400 * 86400:
            return self._send(400, json.dumps({"error": "bad candle request"}).encode(), "application/json")
        key = (sym, res, start // 30, end // 30)
        hit = CANDLE_CACHE.get(key)
        if not hit or time.time() - hit[0] > 30:
            try:
                from .options import API, get
                rows = []
                step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[res] * 1900
                lo = start
                while lo < end:
                    hi = min(end, lo + step)
                    rows += get(f"{API}/v2/history/candles?resolution={res}&symbol={sym}&start={lo}&end={hi}")["result"] or []
                    lo = hi
                data = sorted({int(r["time"]): {"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                                                "close": float(r["close"])} for r in rows}.values(), key=lambda r: r["time"])
                hit = (time.time(), json.dumps(data).encode())
                if len(CANDLE_CACHE) > 200:
                    CANDLE_CACHE.clear()
                CANDLE_CACHE[key] = hit
            except Exception as e:
                return self._send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
        return self._send(200, hit[1], "application/json")

    H._candles = _candles
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    return srv
