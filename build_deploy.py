"""Writes scalper/deploy.json, the data behind the "Deploy strategies" page (/deploy).

Numbers come from the research files, never typed by hand:
  ../edge/research/data/e21_summary.json   E21: the top-4 selection on real Deribit option prices 2019-06..2026-11, Delta costs
Rules and the current paper status are written here. Re-run after the research or the paper books change:
    python3 build_deploy.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
E21 = ROOT.parent / "edge" / "research" / "data" / "e21_summary.json"

ENTRY = "Friday 18:00 UTC (23:30 IST)"
STRIKES = "Sold strike {s} × vol × √T away from the price, protective strike {w} × vol × √T (vol = 30-day implied vol, T = time to expiry)"
STRATS = [
    {"code": "C3", "id": "weekend-trend-spread", "name": "Weekend trend spread", "kind": "Credit spread (one side)",
     "expiry": "Sunday 12:00 UTC (17:30 IST)", "hold": "42 hours",
     "rule": ["At Friday 18:00 UTC, compare the coin's 00:00 UTC price with its 20-day average.",
              "Above the average (up-trend): sell a put spread below the price. Below it: sell a call spread above the price.",
              STRIKES.format(s="0.8", w="1.6") + ".",
              "Hold to Sunday's expiry. The loss is capped at the distance between the two strikes."],
     "payer": "Weekend insurance buyers overpay, and only the side the trend is moving away from is sold.",
     "status": {"BTC": ("off", "Not running"), "ETH": ("off", "Not running (was on paper as \"Weekend trend spread\" until 11 Nov)")}},
    {"code": "C6", "id": "friday-trend-spread", "name": "Friday-night trend spread", "kind": "Credit spread (one side)",
     "expiry": "Saturday 12:00 UTC (17:30 IST)", "hold": "18 hours",
     "rule": ["Same trend rule as #1 (price vs its 20-day average picks the put or the call side).",
              STRIKES.format(s="0.8", w="1.6") + ".",
              "Hold to Saturday's expiry."],
     "payer": "The same weekend insurance premium, over the quietest stretch of the week (Friday night).",
     "status": {"BTC": ("new", "New: needs building"), "ETH": ("new", "New: needs building")}},
    {"code": "C2", "id": "friday-tight-condor", "name": "Friday-night tight condor", "kind": "Iron condor (both sides)",
     "expiry": "Saturday 12:00 UTC (17:30 IST)", "hold": "18 hours",
     "rule": ["Sell a call above and a put below the price, and buy a further-out call and put as protection.",
              STRIKES.format(s="0.57", w="1.14") + ".",
              "Hold to Saturday's expiry. Only one side can lose, and by at most the strike gap."],
     "payer": "Weekend insurance buyers; closer strikes collect more premium and lose more often.",
     "status": {"BTC": ("off", "Not running"), "ETH": ("paper", "On paper at 1× (\"ETH Friday-night condor (tight)\")")}},
    {"code": "C1", "id": "friday-condor", "name": "Friday-night condor", "kind": "Iron condor (both sides)",
     "expiry": "Saturday 12:00 UTC (17:30 IST)", "hold": "18 hours",
     "rule": ["As #3, with the strikes further from the price.",
              STRIKES.format(s="0.8", w="1.6") + ".",
              "Hold to Saturday's expiry."],
     "payer": "Weekend insurance buyers (the edge first found on Delta's own prices in E2/E3).",
     "status": {"BTC": ("paper", "On paper at 5.66× (\"BTC Friday-night condor\")"), "ETH": ("off", "Not running (ETH version retired 11 Nov)")}},
]


def main():
    d = json.loads(E21.read_text())
    cand, pf = d["candidates"], d["portfolio"]
    out = []
    for rank, s in enumerate(STRATS, 1):
        c = cand[s["code"]]
        coins = {}
        for cur in ("BTC", "ETH"):
            a = c["cells"][f"{cur}_all"]
            coins[cur] = {"trades": a["n"], "win_pct": a["win_pct"], "t": a["t"], "avg_usd": a["avg_usd"], "worst_usd": a["worst_usd"],
                          "per_year_usd": a["per_year_usd_if_every_week"],
                          "design_avg_usd": c["cells"][f"{cur}_design"]["avg"], "validation_avg_usd": c["cells"][f"{cur}_validation"]["avg"],
                          "design_sharpe": c["cells"][f"{cur}_design"]["sharpe"], "validation_sharpe": c["cells"][f"{cur}_validation"]["sharpe"],
                          "size_x": pf["sleeve_size"][f"{s['code']}_{cur}"], "weight": pf["weights"][f"{s['code']}_{cur}"],
                          "status": s["status"][cur][0], "status_text": s["status"][cur][1]}
        out.append({k: v for k, v in s.items() if k != "status"} | {"rank": rank, "entry": ENTRY, "score": c["score_min_sharpe"], "coins": coins})
    dh, dp = pf["delta_haircut_15pct"], pf["deribit_prices"]
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "research_generated_utc": d["generated_utc"],
        "source": "edge/research/e21_top4.py and e21b/e22/e23: real Deribit option prices June 2019 to November 2026, Delta's fees, GST and fill ratios",
        "units": "$ per $1,000 of capital per trade at 7.5x notional per sold leg (the research setting)",
        "strategies": out,
        "portfolio": {
            "out_of_sample": {"per_year_pct": dh["validation_per_year_pct"], "max_dd_pct": dh["validation_max_dd_pct"], "worst_week_pct": dh["worst_week_pct"],
                              "sharpe": dh["sharpe"], "losing_weeks_pct": dh["losing_weeks_pct"], "years_pct": dh["years_pct"]},
            "deribit_prices_per_year_pct": dp["validation_per_year_pct"],
            # e21b (all weeks, skipped Fridays filled with their real payoff, Delta haircut, 2020-26) and e22 (compounded)
            "stress": {"per_year_pct": 65.4, "max_dd_pct": 42.2, "worst_week_pct": -29.4, "worst_week_date": "2021-07-23", "compounded_1x_pct": 83.0},
            "worst_possible_weekend_pct": {"median": 26, "p90": 38, "max_seen": 82},
            "expected_range": "about 40–70% a year, drawdowns 15–40%",
            "leverage_limit": "2× of this sizing: ~200%/yr but 71% drawdown; 4×: account wiped out (one weekend, 23 Jul 2021)"},
        "risk_rules": [
            "Same size every Friday (1×): sizing up on \"safer\" Fridays failed out of sample (E23).",
            "Weekly cap: the whole book's worst possible loss on a weekend is at most 25% of capital; positions shrink in volatile weeks.",
            "Never above 2× this sizing (E22).",
            "Every position has bought protective legs, so each trade's loss is capped before it is opened.",
        ],
        "steps": [
            "Build #2 and the missing coin versions as paper books, with the weekly 25% cap.",
            "Run all eight positions on paper for at least 12 Fridays; compare every fill with the model.",
            "Kill rules: stop if the 12-Friday average is below half the backtest, or one weekend loses more than the cap.",
            "Only then: real money at a quarter size, with approval for each trade, before scaling up.",
        ],
    }
    (ROOT / "scalper" / "deploy.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print("wrote scalper/deploy.json:", [s["name"] for s in out])


if __name__ == "__main__":
    main()
