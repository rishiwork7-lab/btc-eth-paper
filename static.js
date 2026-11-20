// Static-site adapter (GitHub Pages). The two pages are the same files the local app serves; this maps their /api/... calls:
// - saved state (books, advisor, trades) -> data/*.json, written by cloud.py on every scheduled run (~15 min);
// - prices and candles -> Delta Exchange's public API, straight from the browser (Delta allows it), so they are live;
// - links like /coin/BTC -> index.html#/coin/BTC (a static host has no server-side routes).
(() => {
  const DELTA = "https://api.india.delta.exchange";
  const realFetch = window.fetch.bind(window);
  const json = async url => { const r = await realFetch(url); if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`); return r.json(); };
  const data = name => json(`data/${name}?t=${Math.floor(Date.now() / 60000)}`);     // one-minute cache-buster
  const reply = obj => new Response(JSON.stringify(obj), {status: 200, headers: {"Content-Type": "application/json"}});
  const bp = (a, b) => (a / b - 1) * 1e4;

  async function deltaCandles(sym, res, start, end) {
    const step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[res] * 1900, rows = {};
    for (let lo = start; lo < end; lo += step) {
      const d = await json(`${DELTA}/v2/history/candles?resolution=${res}&symbol=${encodeURIComponent(sym)}&start=${lo}&end=${Math.min(end, lo + step)}`);
      for (const r of d.result || []) rows[r.time] = {time: +r.time, open: +r.open, high: +r.high, low: +r.low, close: +r.close};
    }
    return Object.values(rows).sort((a, b) => a.time - b.time);
  }

  const prices = {};
  async function livePrices() {
    try {
      const d = await json(`${DELTA}/v2/tickers?contract_types=perpetual_futures`);
      for (const r of d.result || []) if (r.symbol === "BTCUSD" || r.symbol === "ETHUSD") {
        const q = r.quotes || {};
        prices[r.symbol] = q.best_bid && q.best_ask ? (+q.best_bid + +q.best_ask) / 2 : +r.mark_price;
      }
    } catch (e) { /* keep the last prices */ }
    return prices;
  }
  const markR = (x, px) => { if (x.outcome || !x.entry || !px) return; x.mark_r = Math.round((x.dir || (x.side === "BUY" ? 1 : -1)) * bp(px, x.entry) / x.stop_bp * 1000) / 1000; };

  async function advisor(source) {
    const [d, px] = await Promise.all([data(source === "history" ? "advisor_history.json" : "advisor.json"), livePrices()]);
    d.now = Date.now() / 1000;
    for (const s in d.coins) if (px[s]) d.coins[s].price = px[s];
    for (const x of [...(d.open || []), ...(d.recs || [])]) markR(x, px[x.symbol]);
    return d;
  }

  async function bars(sym, start, end) {
    const now = Math.floor(Date.now() / 1000);
    if (!(start && end)) { end = now; start = now - 720 * 60; }
    const [all, chart, px] = await Promise.all([deltaCandles(sym, "1m", start, Math.min(end, now)), data(`chart_${sym}.json`), livePrices()]);
    const closed = all.filter(c => c.time + 60 <= now);
    if (!closed.length) return {bars: [], trades: []};
    const lo = closed[0].time, hi = closed[closed.length - 1].time + 60;
    const trades = chart.filter(t => t.t <= hi && t.end >= lo);
    trades.forEach(t => markR(t, px[sym]));
    return {bars: closed, trades};
  }

  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith("/api/")) return realFetch(input, init);
    const u = new URL(url, location.origin), q = Object.fromEntries(u.searchParams);
    try {
      if (u.pathname === "/api/options") return reply(await data("options.json"));
      if (u.pathname === "/api/breakout") return reply(await data("breakout.json"));
      if (u.pathname === "/api/candles") return reply(await deltaCandles(q.symbol, q.resolution, +q.start, +q.end));
      if (u.pathname === "/api/advisor/trades") return reply(await data(`trades_${q.symbol || "BTCUSD"}.json`));
      if (u.pathname === "/api/advisor/bars") return reply(await bars(q.symbol || "BTCUSD", +q.start || 0, +q.end || 0));
      if (u.pathname === "/api/advisor") return reply(await advisor(q.source));
      return new Response(JSON.stringify({error: "not found"}), {status: 404});
    } catch (e) {
      return new Response(JSON.stringify({error: e.message}), {status: 502, headers: {"Content-Type": "application/json"}});
    }
  };

  // links written for the local server (/options, /coin/BTC, /strategy/x, /advisor) -> this site's files
  const fix = a => {
    const h = a.getAttribute("href");
    if (!h || !h.startsWith("/")) return;
    a.setAttribute("href", h === "/advisor" ? "advisor.html" : h === "/breakout" ? "breakout.html" : h === "/options" ? "index.html" : `index.html#${h}`);
  };
  const fixAll = root => root.querySelectorAll && root.querySelectorAll('a[href^="/"]').forEach(fix);
  document.addEventListener("DOMContentLoaded", () => {
    fixAll(document);
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if (n.nodeType === 1) { if (n.matches('a[href^="/"]')) fix(n); fixAll(n); } })))
      .observe(document.body, {childList: true, subtree: true});
    const S = window.STATIC_SITE, bar = document.createElement("div");
    bar.style.cssText = "background:#eaf0ff;color:#1f3a8a;font:13px/1.4 -apple-system,Segoe UI,sans-serif;padding:6px 16px;text-align:center";
    const age = () => {
      const m = Math.round((Date.now() / 1000 - S.updated) / 60);
      bar.textContent = `Online copy · paper only · prices and candles are live from Delta; books and advisor were last updated ${m} min ago (${S.updated_utc})` +
        (m > 45 ? ". GitHub starts these runs late at busy times (gaps of 40-90 min happen); ideas that start and finish between two runs appear under \"Recently finished\"." : ", by a GitHub run every 15-75 min.");
      bar.style.background = m > 45 ? "#fff3e0" : "#eaf0ff";
    };
    age();
    setInterval(age, 60000);
    document.body.prepend(bar);
  });
  if (!/(advisor|breakout)\.html$/.test(location.pathname)) window.addEventListener("hashchange", () => location.reload());   // the portfolio page reads its route once
})();
