// Weekend option signals for the four deploy strategies (edge/research E21), as pure functions so that the browser page
// (signals.html) and the tests (tests/signals_core.test.js) run the same code. Rules match the paper books in
// scalper/options.py: entry Friday 18:00 UTC; strikes = spot x (1 +/- k x DVOL x sqrt(hold hours / 8760)), nearest listed
// strike for the sold leg, protective leg strictly further out; trend = index now vs its 20-day average.
(function (root) {
  "use strict";

  const STRATS = [
    {rank: 1, id: "weekend-trend-spread", code: "C3", name: "Weekend trend spread", kind: "trend", ks: 0.8, kw: 1.6, expiryDays: 2, holdH: 42},
    {rank: 2, id: "friday-trend-spread", code: "C6", name: "Friday-night trend spread", kind: "trend", ks: 0.8, kw: 1.6, expiryDays: 1, holdH: 18},
    {rank: 3, id: "friday-tight-condor", code: "C2", name: "Friday-night tight condor", kind: "condor", ks: 0.57, kw: 1.14, expiryDays: 1, holdH: 18},
    {rank: 4, id: "friday-condor", code: "C1", name: "Friday-night condor", kind: "condor", ks: 0.8, kw: 1.6, expiryDays: 1, holdH: 18},
  ];
  const COINS = ["BTC", "ETH"];
  const INDEX = {BTC: ".DEXBTUSD", ETH: ".DEETHUSD"};
  const CONTRACT = {BTC: 0.001, ETH: 0.01};        // underlying per option contract on Delta India
  const GRID = {BTC: 200, ETH: 20};                // strike spacing of Delta's daily options (used before a chain is listed)
  const ENTRY_UTC_H = 18, WINDOW_MIN = 30, CAP_PCT = 25;
  const SELL_FLOOR = 0.96, BUY_CEIL = 1.04;        // research fill model: do not sell below 96% of mark or buy above 104%
  const FEE = {rate: 0.0001, cap: 0.035, gst: 0.18};   // Delta India options: min(0.01% notional, 3.5% premium) + 18% GST

  const H = 3600e3, DAY = 24 * H;

  /** Friday 18:00 UTC of the current weekend: the latest Friday's until its Sunday 12:00 UTC expiry, then next Friday's. */
  function entryTime(now) {
    const d = new Date(now);
    const since = (d.getUTCDay() - 5 + 7) % 7;                       // days since the most recent Friday (0 on a Friday)
    const fri = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), ENTRY_UTC_H) - since * DAY;
    return now >= fri + 42 * H ? fri + 7 * DAY : fri;                // Sunday 12:00 UTC = the weekend's last expiry
  }

  /** "preview" before the window, "enter" during Fri 18:00-18:30 UTC, "holding" until the Sunday expiry. */
  function phase(now) {
    const e = entryTime(now);
    if (now < e) return "preview";
    if (now < e + WINDOW_MIN * 60e3) return "enter";
    return "holding";
  }

  function expiryOf(entry, days) {
    const d = new Date(entry);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 12) + days * DAY;
  }

  function expiryCode(ts) {
    const d = new Date(ts), p = x => String(x).padStart(2, "0");
    return p(d.getUTCDate()) + p(d.getUTCMonth() + 1) + p(d.getUTCFullYear() % 100);
  }

  function near(listed, x) {
    return listed.reduce((b, k) => (Math.abs(k - x) < Math.abs(b - x) ? k : b), listed[0]);
  }

  /** Iron condor strikes (same rule as options.pick_strikes). */
  function pickCondor(listed, spot, hs, hw) {
    if (!listed.length) return null;
    const sc = near(listed, spot * (1 + hs)), sp = near(listed, spot * (1 - hs));
    const out = listed.filter(k => k > sc), inn = listed.filter(k => k < sp);
    if (!out.length || !inn.length || sc <= spot * 1.0005 || sp >= spot * 0.9995) return null;
    return {short_call: sc, short_put: sp, wing_call: near(out, spot * (1 + hw)), wing_put: near(inn, spot * (1 - hw))};
  }

  /** Trend spread strikes (same rule as options.pick_spread): up-trend -> put spread, else call spread. */
  function pickSpread(listed, spot, hs, hw, up) {
    if (!listed.length) return null;
    if (up) {
      const sp = near(listed, spot * (1 - hs)), inn = listed.filter(k => k < sp);
      return !inn.length || sp >= spot * 0.9995 ? null : {short_put: sp, wing_put: near(inn, spot * (1 - hw))};
    }
    const sc = near(listed, spot * (1 + hs)), out = listed.filter(k => k > sc);
    return !out.length || sc <= spot * 1.0005 ? null : {short_call: sc, wing_call: near(out, spot * (1 + hw))};
  }

  /** Listed-looking strikes around spot, for previews before Delta lists the expiry. */
  function gridStrikes(coin, spot, span = 0.25) {
    const g = GRID[coin], lo = Math.floor(spot * (1 - span) / g) * g, hi = Math.ceil(spot * (1 + span) / g) * g, out = [];
    for (let k = lo; k <= hi; k += g) out.push(k);
    return out;
  }

  function optionFee(px, notional) {
    return (1 + FEE.gst) * Math.min(FEE.rate * notional, FEE.cap * px);
  }

  /**
   * One position (strategy x coin).
   * m = {spot, dvol (in %), avg20, entry (ms), chain: {symbol: {bid, ask, mark}} or null}; sizeX = notional per sold leg / capital.
   */
  function buildPosition(s, coin, m, capital, sizeX) {
    const T = s.holdH / 8760, sig = m.dvol / 100 * Math.sqrt(T);
    const hs = s.ks * sig, hw = s.kw * sig;
    const expiry = expiryOf(m.entry, s.expiryDays), code = expiryCode(expiry);
    const sym = (cp, k) => `${cp}-${coin}-${k}-${code}`;
    const chain = m.chain || {};
    const listedC = Object.keys(chain).filter(x => x.startsWith("C-") && x.endsWith("-" + code)).map(x => +x.split("-")[2]);
    const listedP = Object.keys(chain).filter(x => x.startsWith("P-") && x.endsWith("-" + code)).map(x => +x.split("-")[2]);
    const both = listedC.filter(k => listedP.includes(k)).sort((a, b) => a - b);
    const listed = both.length >= 6;
    const strikes = listed ? both : gridStrikes(coin, m.spot);
    const up = m.avg20 != null ? m.spot > m.avg20 : null;
    if (s.kind === "trend" && up == null) return {s, coin, error: "20-day average unavailable"};
    const k = s.kind === "trend" ? pickSpread(strikes, m.spot, hs, hw, up) : pickCondor(strikes, m.spot, hs, hw);
    if (!k) return {s, coin, error: "no suitable strikes"};
    const spec = [["wing_call", "C", "buy"], ["wing_put", "P", "buy"], ["short_call", "C", "sell"], ["short_put", "P", "sell"]]
      .filter(([n]) => n in k);                                   // protective legs first: never leave a sold option uncovered
    const notional = sizeX * capital, qty = notional / m.spot;
    const contracts = Math.floor(qty / CONTRACT[coin] + 1e-9);
    const legs = spec.map(([name, cp, side]) => {
      const q = chain[sym(cp, k[name])];
      const mark = q && q.mark != null && isFinite(q.mark) ? q.mark : null;
      return {name, side, call: cp === "C", strike: k[name], symbol: sym(cp, k[name]), listed: !!q, mark, bid: q ? q.bid : null, ask: q ? q.ask : null,
              limit: mark == null ? null : side === "sell" ? mark * SELL_FLOOR : mark * BUY_CEIL};
    });
    const sideW = cp => { const w = legs.find(l => l.side === "buy" && l.call === (cp === "C")), sh = legs.find(l => l.side === "sell" && l.call === (cp === "C"));
      return w && sh ? Math.abs(w.strike - sh.strike) : 0; };
    const width = Math.max(sideW("C"), sideW("P"));
    const priced = legs.every(l => l.mark != null);
    const units = contracts * CONTRACT[coin];
    const credit = priced ? legs.reduce((a, l) => a + (l.side === "sell" ? 1 : -1) * l.mark, 0) : null;    // per 1 underlying, at mark
    const fees = priced ? legs.reduce((a, l) => a + optionFee(l.mark * units, m.spot * units), 0) : null;
    const maxLoss = priced ? (width - credit) * units + fees : width * units;                              // before pricing: the width
    const shorts = legs.filter(l => l.side === "sell");
    const be = priced ? {low: shorts.some(l => !l.call) ? shorts.find(l => !l.call).strike - credit : null,
                         high: shorts.some(l => l.call) ? shorts.find(l => l.call).strike + credit : null} : null;
    return {s, coin, code, expiry, up, legs, listed, priced, contracts, units, width, credit, creditUsd: priced ? credit * units : null, fees,
            maxLoss, breakeven: be, notional, sigma: sig};
  }

  /** Weekly cap: scale every position down (whole contracts) until the total worst case is <= CAP_PCT of capital. */
  function applyCap(positions, capital) {
    const ok = positions.filter(p => !p.error && p.contracts > 0);
    const total = ok.reduce((a, p) => a + p.maxLoss, 0), limit = CAP_PCT / 100 * capital;
    const f = total > limit ? limit / total : 1;
    if (f < 1) for (const p of ok) {
      const c = Math.floor(p.contracts * f + 1e-9), r = p.contracts ? c / p.contracts : 0;
      p.capped = true;
      p.contracts = c; p.units = c * CONTRACT[p.coin];
      p.maxLoss *= r; if (p.fees != null) p.fees *= r; if (p.creditUsd != null) p.creditUsd *= r;
    }
    return {total: ok.reduce((a, p) => a + p.maxLoss, 0), limit, scale: f};
  }

  const api = {STRATS, COINS, INDEX, CONTRACT, GRID, CAP_PCT, SELL_FLOOR, BUY_CEIL, entryTime, phase, expiryOf, expiryCode, pickCondor, pickSpread,
               gridStrikes, optionFee, buildPosition, applyCap};
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.Signals = api;
})(typeof window !== "undefined" ? window : globalThis);
