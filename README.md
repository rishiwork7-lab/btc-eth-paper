# Paper strategy books (live Delta data)

## Online copy: https://rishiwork7-lab.github.io/btc-eth-paper/ (from 2026-11-19)

GitHub Actions (`.github/workflows/cloud.yml`) runs `cloud.py` every ~15 minutes (started by an external timer, cron-job.org, through
GitHub's workflow-dispatch API, because GitHub's own schedule ran 40-120 minutes apart; the schedule stays as a backup), so the paper record continues while this
computer is off: the books take one step (a run that starts on Friday from 17:00 UTC stays up, stepping every 15 s, until the
18:00-18:10 UTC condor entry has passed), the advisor catches up on every closed 1-minute candle and learns, and the site is
rebuilt (`site/`: the same two pages plus `static.js`, which maps their `/api` calls to `data/*.json` and to Delta's public API,
so prices and candles are live in the browser). State is the two SQLite files on the `state` branch, replaced on each run.
The online record is its own copy, seeded on 2026-11-19 from this computer's state; the local app (`python3 run.py`) keeps
a separate record if it is left running. Cloud entries for the advisor fill at the next 1-minute open (a run is minutes behind).

## 1-minute trade advisor, page /advisor (added 2026-11-19)

`scalper/advisor.py` + `advisor.html`, started by `run.py`. Recommendations only (no orders, no API key). On every closed
Delta 1-minute candle of BTCUSD and ETHUSD it scans five setup families (range breakout, sweep and reclaim, flush reversal,
trend pullback, session-open drive), gives each candidate an entry, stop, target (2R, at least 3x the worst round-trip cost),
time stop, size (0.25% risk) and the reasons, then follows it to the target, stop or time stop and writes why it worked or
failed. One online model per family learns from every outcome (picked or not) and logs how each outcome moved its weights.

- **Statuses:** SKIPPED (model expects < +0.10 R after costs), UNPROVEN (a pick from a family not yet trusted), BLOCKED (a pick
  stopped by a risk limit), RECOMMENDED (a pick from a trusted family: >= 100 earlier picks, net > 0, t >= 2, PF >= 1.3).
- **Same data live and in the replay:** Delta's own 1-minute candles; the first start replays 180 days in time order, each
  score made before its outcome was known. Missed candles are caught up after a restart.
- **Version L1, frozen 2026-11-19.** Changing families, inputs or thresholds makes L2 with a fresh record.
- **First result (180-day replay):** all five families lost about -0.5 R per candidate after costs (12,823 candidates). The
  model skipped 99%; its 155 picks averaged -0.04 R; following them made +1.8% (worst drawdown 3.3%). Calibration: higher
  scores did not yet mean better results. Nothing is trusted, so nothing is RECOMMENDED.
- **Chart:** every trade the account took (history replay and live) is drawn as a position box: green from entry to target, red
  from entry to stop, as wide as the trade was open, with the exit dot and result. Pick one from the list (or step with
  Older/Newer); a link to one trade is `/advisor#trade=ETHUSD:<id>`. **Current** items in the "Latest 12 h" view: an open trade
  has a bold blue outline, a live "now +x R" label and its entry/stop/target on the price axis; a candidate the model skipped
  but is still following is drawn dashed ("Skipped candidates" toggles it). The minute still forming is drawn from the live price. Since mid-August the model has taken no trade (it
  learned that every family lost), so the latest 12 hours usually show no boxes.
- Files: `advisor/advisor.sqlite` (every idea, outcome, explanation; learning log; model state), `advisor/*_1m.json` (history cache).
- Tests: `tests/test_advisor.py`.

**BTC and ETH only, since 2026-11-11 (owner's rule).** Later that day the owner asked to keep only the parts that perform well, so
**only the BTC Friday-night condor (weight 0.377) and the ETH dip-buy (0.623) run, at 2×** (~34%/yr backtest, worst drawdown
27%). Trend and the ETH condor were removed. The table below is the earlier 4-part version. The runner trades the BE blend: one $10,000 paper account at 1.5×.

| Part | Weight | Code |
|---|---|---|
| BTC Friday-night condor | 0.271 | `options.py`, book `be-btc-condor` |
| ETH Friday-night condor | 0.175 | `options.py`, book `be-eth-condor` |
| Trend on BTC and ETH | 0.101 | `r3.py`, `BETrendSleeve` |
| ETH dip-buy | 0.453 | `futures.py`, book `be-eth-dip` |

- **Backtest:** `edge/research/e13_be_paper.py`, ~26.7%/yr, Sharpe 1.42, worst drawdown 28.2%, 2021–26. It also writes the
  pages' backtest files.
- **Charts:** each coin page has TradingView Lightweight Charts (4.2.0 from unpkg) with every paper fill, the open-position
  levels and the option strikes. Candles come through `/api/candles`.
- **Retired:** R3's other-coin positions were closed at the live bid/ask on 2026-11-11.

**Since 2026-11-11 the runner trades the R3 blend** (the owner's choice after the 50% research loop), in one $10,000 paper
account at 1.5×:
- **Trend:** `scalper/r3.py` TrendSleeve, on 15 Delta perps.
- **Funding carry:** `scalper/r3.py` CarrySleeve. The signal is Binance funding; Delta funding is charged on every position.
- **BTC Friday-night condor:** the `r3-btc-condor` book in `scalper/options.py`.

**Backtest:** `edge/research/e12_r3_paper.py` (~26%/yr, Sharpe 1.26, worst drawdown 27.5%, 2021–26). Then
`python3 build_book_backtests.py r3` builds the panels.

**Pages** (light theme):
- `/options`: overview.
- `/coin/BTC`, `/coin/ETH`, `/coin/OTHER`: each coin's options, then its futures.
- `/strategy/<book>`: each sleeve in full.

The ETH Friday-night condor, ETH trend spread and ETH dip-buy books no longer run; their records stay in the database.

**Live scalping was removed on 2026-11-11 at the owner's request.** The engine now only supplies live prices and the
database writer to the strategy books; the scalper page and its API are gone, and `/` opens the strategy overview. The
scalper code and journal below are kept as the record of why it was retired.

## Former BTC/ETH scalper

The `btc-eth-scalping-agent` skill (`../.claude/skills/btc-eth-scalping-agent/SKILL.md`) running on Delta Exchange India's
live public feed. **PAPER only:** there is no API key, and no real order can be placed.

- **Start:** `python3 run.py`, then open **http://127.0.0.1:8770**
- **Stop:** `pkill -f "scalper/run.py"` (or Ctrl-C). Account, open positions and the full journal survive restarts (`data/scalper.sqlite`).
- **Tests:** `../trading-harness/.venv/bin/python -m pytest -q tests`

What runs:
- **Market data:** one WebSocket to Delta (best bid/ask, every trade, ticker and funding, the BTC and ETH spot indices), plus
  1-minute bars and features: squeeze ratio, realized volatility, ATR, 1h/4h ranges, VWAP, relative volume, order-flow imbalance.
- **Setups:** three written hypotheses in `scalper/setups.py`, all **exploring** (half size) until they have a record.
- **Cost gate:** checked on every signal. Average target must be at least 3× the round-trip cost: fees + GST from Delta's API,
  spread, slippage and funding.
- **Risk limits:** in `config.toml`, the skill's §8 defaults. The agent never edits them.
- **Strict paper fills:** no touch-fills; market orders fill at the quote 300 ms later; stops fill at the first trade beyond
  them minus slippage.
- **Journal:** every signal, no-trade minute, order and trade in SQLite, written only by the writer thread.

It only sees the market while this computer is awake and the runner is up; nothing is replayed after a gap.

## ETH Friday-night condor (paper), page /options

`scalper/options.py` paper-trades the one edge that passed a pre-registered test (`../edge/research`, E2/E3), at 7.5×
notional per leg (the owner's choice, 2026-11-10):
- **Entry:** every Friday at 18:00 UTC (23:30 IST).
- **Trade:** sell an ETH call and put expiring Saturday 12:00 UTC at ±0.8 × DVOL × √T, and buy wings at ±1.6×. The wings cap
  the worst loss before entry.
- **Fills:** sells at the live bid, buys at the live ask.
- **Risk check:** an entry whose worst possible loss is over 25% of capital is refused.
- **Settlement:** at Delta's settlement price.
- **Backtest data:** `backtest/options_backtest*.json`, built from the research rows by `build_book_backtests.py`.

**Strategy pages** (2026-11-11):
- `/options` is the overview.
- `/strategy/fri-night`: Friday-night condor.
- `/strategy/trend-spread`: weekend trend spread (`options.py`, E5 O3).
- `/strategy/eth-dip`: ETH dip-buy, futures (`futures.py`, E5 F2).

The three scalping setups were **retired** on 2026-11-10 after `backtest.py` showed them losing before fees over 3 months (its page was removed on 2026-11-11 at the owner's request).

