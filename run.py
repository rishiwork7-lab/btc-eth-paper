"""Run the PAPER BTC/ETH blend (BTC Friday-night condor + ETH dip-buy; one shared $10,000 at 2x) on Delta Exchange India's live
public data, with their pages. Live scalping was removed on 2026-11-11 at the owner's request; the engine now only supplies
live prices and the database writer.
    python3 run.py            then open http://127.0.0.1:8770
Stop with Ctrl-C (or pkill -f "scalper/run.py"). Positions, account and the journal survive restarts (data/scalper.sqlite)."""
import json
import os
import ssl
import sys
import threading
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from scalper.engine import Engine, utc  # noqa: E402
from scalper.futures import DipBuyer  # noqa: E402
from scalper.r3 import BE_LEVERAGE, BE_WEIGHTS, CarrySleeve, TrendSleeve  # noqa: E402
from scalper.options import RUNNING, FridayCondor  # noqa: E402
from scalper.server import serve  # noqa: E402
from scalper.advisor import Advisor  # noqa: E402
from scalper.ws import WebSocket  # noqa: E402

API, WS = "https://api.india.delta.exchange", "wss://socket.india.delta.exchange"
INDEX = {"BTCUSD": ".DEXBTUSD", "ETHUSD": ".DEETHUSD"}
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem" if os.path.exists("/etc/ssl/cert.pem") else None)


def get(path):
    with urllib.request.urlopen(urllib.request.Request(API + path, headers={"User-Agent": "scalper-paper"}), timeout=30, context=CTX) as r:
        return json.loads(r.read())


def products(symbols):
    """Fees, tick size and contract size from Delta's product API at start-up (skill §4: the fee table is refreshed)."""
    out = {}
    for s in symbols:
        p = get(f"/v2/products/{s}")["result"]
        out[s] = {"tick": float(p["tick_size"]), "contract": float(p["contract_value"]), "maker": float(p["maker_commission_rate"]),
                  "taker": float(p["taker_commission_rate"]), "index": INDEX[s]}
    return out


def main():
    conf = tomllib.loads((ROOT / "config.toml").read_text())
    if conf["mode"] != "PAPER":
        sys.exit("Only PAPER mode exists. Live trading needs the skill's §10 gate and the owner's explicit approval and configuration.")
    eng = Engine(ROOT, products(conf["symbols"]), conf)
    eng.scalping = False
    threading.Thread(target=eng.writer, daemon=True, name="writer").start()
    books = [FridayCondor(eng, capital=conf["capital"], book=b) for b in RUNNING]
    # BTC/ETH blend, one shared $10,000: BTC Friday condor + ETH dip-buy (the two strong parts; trend and ETH condor removed)
    books += [DipBuyer(eng, capital=conf["capital"], book="be-eth-dip", state_key="be_eth_dip", leverage=BE_WEIGHTS["eth_dip"] * BE_LEVERAGE,
                       name="ETH dip-buy (futures)")]
    retired = [TrendSleeve(eng, capital=conf["capital"]), CarrySleeve(eng, capital=conf["capital"])]   # R3 sleeves with other coins: close once

    def retire():
        time.sleep(20)                      # let the writer start
        for r in retired:
            try:
                r.close_all(time.time(), "Owner restricted trading to BTC and ETH only (11 Nov 2026); R3 replaced by the BTC/ETH blend")
            except Exception as e:
                print(f"{utc(time.time())} could not retire {r.book}: {e}", flush=True)
    threading.Thread(target=retire, daemon=True, name="retire-r3").start()
    for b in books:
        threading.Thread(target=b.loop, daemon=True, name=f"book-{b.book}").start()
    def quote(sym):
        i = eng.inst.get(sym)
        return (i.bid, i.ask, time.time() - i.l1_rx) if i and i.bid and i.ask else None
    # 1-minute advisor (page /advisor): recommendations with reasons, outcomes and live learning; paper only, no orders
    adv = Advisor(ROOT, eng.fees, conf["limits"], quote=quote, slippage_bp=conf["costs"]["slippage_bp"], events=conf.get("events", {}).get("times", []))
    threading.Thread(target=adv.start, daemon=True, name="advisor").start()
    serve(eng, conf["port"], books, adv)
    print(f"{utc(time.time())} PAPER strategy books up; http://127.0.0.1:{conf['port']}", flush=True)
    sub = {"type": "subscribe", "payload": {"channels": [{"name": n, "symbols": conf["symbols"]} for n in ("l1_orderbook", "all_trades", "v2/ticker")]
                                            + [{"name": "v2/spot_price", "symbols": [INDEX[s] for s in conf["symbols"]]}]}}
    backoff, last_save = 1.0, 0.0
    while True:
        try:
            ws = WebSocket(WS)
            ws.send_json(sub)
            print(f"{utc(time.time())} connected to Delta", flush=True)
            backoff = 1.0
            while True:
                eng.on_message(json.loads(ws.recv()))
                if time.time() - last_save > 5:
                    with eng.lock:
                        eng.snapshot_state()
                    last_save = time.time()
        except KeyboardInterrupt:
            with eng.lock:
                eng.snapshot_state()
            time.sleep(1)
            print("stopped")
            return
        except Exception as e:  # reconnect; open positions keep their stops; new entries wait for fresh quotes (staleness check)
            print(f"{utc(time.time())} feed lost ({type(e).__name__}: {e}); reconnecting in {backoff:.0f} s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    main()
