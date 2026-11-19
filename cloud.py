"""One scheduled run of the PAPER books and the 1-minute advisor, then the static website. Runs on GitHub Actions
(.github/workflows/cloud.yml) every ~15 minutes, so the record keeps going while this computer is off.

    python3 cloud.py                 one run: books, advisor catch-up, site/ built
    python3 cloud.py --site-only     rebuild site/ from the saved state (no trading)

What one run does:
- quotes: Delta's REST tickers fill the engine's instruments (best bid/ask, index, mark, funding), stamped now;
- books: each running book takes one step (the same step() the live runner calls every 15-30 s). Around the Friday
  18:00 UTC condor entry a run that starts from 17:00 UTC stays up and steps every 15 s until 18:12 UTC, so the window is not missed;
- advisor: catches up on every 1-minute candle closed since its last one (source 'cloud': entries fill at the next candle's
  open, as in the replay, because a run is minutes behind the market), learns from the outcomes, saves its state;
- site: site/index.html (books) and site/advisor.html with data/*.json; the pages fetch live prices and candles from Delta
  in the browser (Delta allows that), so they move between runs.
State (data/scalper.sqlite, advisor/advisor.sqlite) is kept on the repository's 'state' branch by the workflow."""
import json
import shutil
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from run import API, get, products  # noqa: E402
from scalper.advisor import SYMBOLS, Advisor, utc  # noqa: E402
from scalper.engine import Engine  # noqa: E402
from scalper.futures import DipBuyer  # noqa: E402
from scalper.options import RUNNING, FridayCondor  # noqa: E402
from scalper.r3 import BE_LEVERAGE, BE_WEIGHTS  # noqa: E402
from scalper.server import options_payload  # noqa: E402

SITE = ROOT / "site"
# Friday, seconds after 00:00 UTC: any run that starts in here stays up, stepping every 15 s, until the 18:00-18:10 UTC condor
# entry window has passed. Scheduled runs can start late or be skipped, so the window opens an hour early.
ENTRY_WINDOW = (17 * 3600, 18 * 3600 + 12 * 60)


def refresh_quotes(eng: Engine):
    rows = get("/v2/tickers?contract_types=perpetual_futures")["result"]
    now = time.time()
    for r in rows:
        i = eng.inst.get(r["symbol"])
        q = r.get("quotes") or {}
        if not i or not q.get("best_bid") or not q.get("best_ask"):
            continue
        i.bid, i.ask = float(q["best_bid"]), float(q["best_ask"])
        i.bq, i.aq = float(q.get("bid_size") or 0), float(q.get("ask_size") or 0)
        i.l1_rx = now
        if r.get("spot_price"):
            i.index, i.index_rx = float(r["spot_price"]), now
        i.mark = float(r["mark_price"]) if r.get("mark_price") else i.mark
        i.funding = float(r["funding_rate"]) if r.get("funding_rate") not in (None, "") else i.funding


def in_entry_window(now: float) -> bool:
    d = datetime.fromtimestamp(now, timezone.utc)
    s = d.hour * 3600 + d.minute * 60 + d.second
    return d.weekday() == 4 and ENTRY_WINDOW[0] <= s < ENTRY_WINDOW[1]


def step_books(eng: Engine, books: list):
    refresh_quotes(eng)
    for b in books:
        try:
            b.step(time.time())
        except Exception as e:  # recorded on the book's page, never silent
            b._event(time.time(), "ERROR", f"{type(e).__name__}: {e}")
    eng.flush()


def build_site(eng: Engine, books: list, adv: Advisor, run_note: str):
    """site/: the two pages (unchanged code, plus static.js which maps their /api calls to data/*.json and Delta) and data."""
    shutil.rmtree(SITE, ignore_errors=True)
    (SITE / "data").mkdir(parents=True)
    now = time.time()
    meta = {"updated": int(now), "updated_utc": utc(now) + " UTC", "note": run_note}
    inject = f'<script>window.STATIC_SITE = {json.dumps(meta)};</script>\n<script src="static.js"></script>\n'
    for src, dst in (("options.html", "index.html"), ("advisor.html", "advisor.html")):
        html = (ROOT / "scalper" / src).read_text()
        (SITE / dst).write_text(html.replace("<head>\n", "<head>\n" + inject, 1))
    shutil.copy(ROOT / "static.js", SITE / "static.js")
    (SITE / ".nojekyll").write_text("")

    def put(name, obj):
        (SITE / "data" / name).write_text(json.dumps(obj, default=str, separators=(",", ":")))
    put("options.json", options_payload(eng, books))
    adv.phase = f"online · updated {meta['updated_utc']}, every ~15 min"
    put("advisor.json", adv.view("live"))
    put("advisor_history.json", adv.view("history"))
    for s in SYMBOLS:
        put(f"trades_{s}.json", adv.trades(s))
        put(f"chart_{s}.json", adv.trades(s, shadow=True))
    put("status.json", meta)


def main():
    conf = tomllib.loads((ROOT / "config.toml").read_text())
    if conf["mode"] != "PAPER":
        sys.exit("Only PAPER mode exists.")
    eng = Engine(ROOT, products(conf["symbols"]), conf)
    eng.scalping = False
    books = [FridayCondor(eng, capital=conf["capital"], book=b) for b in RUNNING]
    books += [DipBuyer(eng, capital=conf["capital"], book="be-eth-dip", state_key="be_eth_dip", leverage=BE_WEIGHTS["eth_dip"] * BE_LEVERAGE,
                       name="ETH dip-buy (futures)")]
    adv = Advisor(ROOT, eng.fees, conf["limits"], quote=None, slippage_bp=conf["costs"]["slippage_bp"], events=conf.get("events", {}).get("times", []))
    note = "site rebuilt from saved state"
    if "--site-only" not in sys.argv:
        step_books(eng, books)
        steps = 1
        while in_entry_window(time.time()):         # stay up through the Friday condor entry window
            time.sleep(15)
            step_books(eng, books)
            steps += 1
        if not any(adv.last_t.values()):
            sys.exit("advisor has no saved state: seed advisor/advisor.sqlite first (the replay is not run in the cloud)")
        adv.catch_up("cloud")
        adv.flush()
        note = f"books stepped {steps}x; advisor caught up to {utc(max(adv.last_t.values()) + 60)} UTC"
        print(note, flush=True)
    for db_path in (eng.dbpath, adv.dbpath):        # fold the write-ahead logs into the files the workflow saves
        db = sqlite3.connect(str(db_path))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
    build_site(eng, books, adv, note)
    print(f"site built: {sum(1 for _ in SITE.rglob('*'))} files", flush=True)


if __name__ == "__main__":
    main()
