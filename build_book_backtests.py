"""Build the backtest panels for the paper books from the research results (no new testing happens here).

- backtest/options_backtest_trend-spread.json  <- edge/research E5 O3 (Delta's own ETH option marks) + its 5-year replay
- backtest/options_backtest_eth-dip.json       <- edge/research E5 F2 (ETH perp, 2020-2026)
- backtest/options_backtest.json               <- adds the 5-year replay of the Friday-night condor (E5 O1)
Money figures are on $10,000 of paper capital, fixed (no compounding).
Run: python3 build_book_backtests.py
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RES = ROOT.parent / "edge" / "research" / "data"
BT = ROOT / "backtest"
CAP = 10000.0


def stats(rows, per_year):
    xs = [r["pnl"] for r in rows]
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    eq = peak = dd = 0.0
    for x in xs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wins, losses = [x for x in xs if x > 0], [x for x in xs if x <= 0]
    return {"fridays": n, "trades": n, "from": rows[0]["expiry"], "to": rows[-1]["expiry"], "net": round(sum(xs), 2), "return_pct": round(sum(xs) / CAP * 100, 2),
            "per_year_pct": round(m * per_year / CAP * 100, 1), "win_rate": round(len(wins) / n * 100, 1),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "worst": round(min(xs), 2), "best": round(max(xs), 2), "max_dd": round(dd, 2), "max_dd_pct": round(dd / CAP * 100, 2),
            "t_stat": round(m / sd * math.sqrt(n), 2) if sd else None}


def stress(rows):
    xs = [r["pnl"] for r in rows]
    years = {}
    for r in rows:
        years.setdefault(r["expiry"][:4], []).append(r["pnl"])
    eq = peak = dd = 0.0
    for x in xs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return {"weeks": len(xs), "from": rows[0]["expiry"], "per_year_pct": round(m * 52 / 10, 1), "t_stat": round(m / sd * math.sqrt(len(xs)), 2),
            "worst_pct": round(min(xs) / 10, 1), "max_dd_pct": round(dd / 10, 1),
            "years": {y: round(sum(v) / 10, 1) for y, v in sorted(years.items())},
            "note": "Synthetic: Delta option prices do not exist before Oct 2025, so legs are priced from Deribit DVOL calibrated to Delta's marks. "
                    "On the 13 months where both exist, the replay averaged ~$3 per $1,000 a week less than Delta's real prices."}


def scaled(rows):
    return [r | {"pnl": r["pnl"] * CAP / 1000} for r in rows]


def last3(rows):
    return [r for r in rows if r["expiry"] >= "2026-08-10"]


def main():
    o = json.loads((RES / "e5_options_summary.json").read_text())
    s3 = json.loads((RES / "e5_o3_stress.json").read_text())
    f = json.loads((RES / "e5_futures_summary.json").read_text())

    rows = scaled(sorted(o["O3 Trend spread | ETH"]["rows"], key=lambda r: r["expiry"]))
    out = {"source": "edge/research/e5_options.py (O3, pre-registered), Delta's own ETH option marks", "capital": CAP, "leverage": 7.5,
           "full": stats(rows, 52) | {"worst_possible": 2500.0}, "last_3_months": stats(last3(rows), 52) | {"worst_possible": 2500.0},
           "stress_5y": stress(s3["ETH"]["rows"]), "rows": [{"expiry": r["expiry"], "pnl": round(r["pnl"], 2), "side": r["side"]} for r in rows]}
    (BT / "options_backtest_trend-spread.json").write_text(json.dumps(out, indent=1))

    fn = BT / "options_backtest.json"
    fri = json.loads(fn.read_text())
    sys.path.insert(0, str(RES.parent))
    import e5_options as e5  # cached data: no downloads after the first E5 run
    c = o["O1 Friday night 2021-26 | ETH"]["calibration_c"]
    fri["stress_5y"] = stress(e5.synthetic("ETH", e5.long_dvol("ETH"), c, 18, 5))
    fn.write_text(json.dumps(fri, indent=1))

    dip = [{"expiry": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), "pnl": round(r * CAP, 2)} for t, r in f["F2 Dip in uptrend | ETH"]["rows"]]
    years = (datetime.fromisoformat(dip[-1]["expiry"]) - datetime(2020, 6, 14)).days / 365.25   # the first possible signal needs 200 days of closes
    out = {"source": "edge/research/e5_futures.py (F2, pre-registered), ETH perpetual daily closes 2019-11 to 2026-11", "capital": CAP, "leverage": 1.0,
           "full": stats(dip, len(dip) / years), "last_3_months": None, "years": {}, "rows": dip}
    for r in dip:
        out["years"][r["expiry"][:4]] = round(out["years"].get(r["expiry"][:4], 0) + r["pnl"] / CAP * 100, 1)
    (BT / "options_backtest_eth-dip.json").write_text(json.dumps(out, indent=1))
    print("trend-spread", json.dumps(json.loads((BT / "options_backtest_trend-spread.json").read_text())["full"]))
    print("eth-dip", json.dumps(out["full"]), out["years"])
    print("fri stress", json.dumps(fri["stress_5y"]))


def build_r3():
    """Backtest panels for the R3 paper books, from edge/research/e12_r3_paper.py (run that first)."""
    r3 = json.loads((BT / "r3_backtest.json").read_text())
    real = r3["condor_real"]
    syn = r3["condor_synthetic_rows"]
    xs = [r["pnl"] for r in syn]
    eq = peak = dd = 0.0
    years = {}
    for r in syn:
        eq += r["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        years[r["expiry"][:4]] = round(years.get(r["expiry"][:4], 0) + r["pnl"] / 100, 1)
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    condor = {"source": "BTC Friday-night condor at the R3 sleeve size (5.06x notional per leg)", "capital": CAP, "leverage": round(7.5 * 0.45 * 1.5, 2),
              "full": stats(real, 52) | {"worst_possible": None}, "last_3_months": stats(last3(real), 52) | {"worst_possible": None}, "rows": real,
              "stress_5y": {"weeks": len(xs), "from": syn[0]["expiry"], "per_year_pct": round(m * 52 / 100, 1), "t_stat": round(m / sd * math.sqrt(len(xs)), 2),
                            "worst_pct": round(min(xs) / 100, 1), "max_dd_pct": round(dd / 100, 1), "years": years,
                            "note": "Synthetic: Delta option prices do not exist before Oct 2025, so legs are priced from Deribit DVOL calibrated to Delta's marks."}}
    (BT / "options_backtest_r3-btc-condor.json").write_text(json.dumps(condor, indent=1))
    for key, book in (("T", "r3-trend"), ("C", "r3-carry")):
        (BT / f"options_backtest_{book}.json").write_text(json.dumps({"full": r3["components"][key], "rows": r3["component_rows"][key], "weights": r3["weights"],
                                                                       "leverage": r3["leverage"]}, indent=1))
    print("R3 books built:", json.dumps(r3["blend"]))


if __name__ == "__main__":
    if sys.argv[1:] == ["r3"]:
        build_r3()
    else:
        main()
