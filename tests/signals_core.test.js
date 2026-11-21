// node --test tests/signals_core.test.js   (also run from tests/test_signals.py)
const test = require("node:test");
const assert = require("node:assert");
const S = require("../scalper/signals_core.js");

const T = (s) => Date.parse(s);

test("phase and entry time follow Friday 18:00 UTC and the Sunday 12:00 UTC expiry", () => {
  assert.strictEqual(S.phase(T("2026-11-20T17:59:00Z")), "preview");
  assert.strictEqual(S.phase(T("2026-11-20T18:05:00Z")), "enter");
  assert.strictEqual(S.phase(T("2026-11-20T18:31:00Z")), "holding");
  assert.strictEqual(S.phase(T("2026-11-21T13:00:00Z")), "holding");                      // Saturday: still last night's trades
  assert.strictEqual(S.entryTime(T("2026-11-21T13:00:00Z")), T("2026-11-20T18:00:00Z"));
  assert.strictEqual(S.entryTime(T("2026-11-22T12:00:00Z")), T("2026-11-27T18:00:00Z"));  // Sunday expiry passed: next Friday
  assert.strictEqual(S.entryTime(T("2026-11-25T09:00:00Z")), T("2026-11-27T18:00:00Z"));  // Wednesday
  assert.strictEqual(S.expiryCode(S.expiryOf(T("2026-11-20T18:00:00Z"), 1)), "211126");
  assert.strictEqual(S.expiryCode(S.expiryOf(T("2026-11-20T18:00:00Z"), 2)), "221126");
});

test("condor strikes reproduce the paper book's 20 Nov BTC trade", () => {
  // paper book (be-btc-condor) entered at spot 59,129.8 with DVOL 52.11: 58000/60200 sold, 56800/61400 bought
  const strikes = []; for (let k = 50000; k <= 70000; k += 200) strikes.push(k);
  const sig = 0.5211 * Math.sqrt(18 / 8760);
  const k = S.pickCondor(strikes, 59129.8, 0.8 * sig, 1.6 * sig);
  assert.deepStrictEqual(k, {short_call: 60200, short_put: 58000, wing_call: 61400, wing_put: 56800});
});

test("trend spread sells the side away from the trend", () => {
  const strikes = []; for (let k = 1500; k <= 1900; k += 20) strikes.push(k);
  const up = S.pickSpread(strikes, 1700, 0.02, 0.04, true), dn = S.pickSpread(strikes, 1700, 0.02, 0.04, false);
  assert.ok(up.short_put < 1700 && up.wing_put < up.short_put && !("short_call" in up));
  assert.ok(dn.short_call > 1700 && dn.wing_call > dn.short_call && !("short_put" in dn));
});

test("a priced position: protective legs first, sizes in whole contracts, worst loss = width - credit + fees", () => {
  const entry = T("2026-11-27T18:00:00Z"), code = "281126", chain = {};
  for (let k = 50000; k <= 70000; k += 200) {
    chain[`C-BTC-${k}-${code}`] = {mark: Math.max(1, 60000 - k + 300), bid: 0, ask: 0};
    chain[`P-BTC-${k}-${code}`] = {mark: Math.max(1, k - 60000 + 300), bid: 0, ask: 0};
  }
  const s = S.STRATS.find(x => x.code === "C1");
  const p = S.buildPosition(s, "BTC", {spot: 60000, dvol: 50, avg20: 58000, entry, chain}, 10000, 1.22);
  assert.ok(!p.error && p.listed && p.priced);
  assert.deepStrictEqual(p.legs.map(l => l.side), ["buy", "buy", "sell", "sell"]);
  assert.strictEqual(p.contracts, Math.floor(1.22 * 10000 / 60000 / 0.001));
  assert.ok(p.legs.every(l => l.symbol.endsWith("-" + code)));
  assert.ok(Math.abs(p.maxLoss - ((p.width - p.credit) * p.units + p.fees)) < 1e-9 && p.maxLoss > 0);
  assert.ok(p.legs.filter(l => l.side === "sell").every(l => l.limit === l.mark * 0.96));
});

test("before Delta lists the expiry, a preview still shows target strikes on the usual grid", () => {
  const s = S.STRATS.find(x => x.code === "C3");
  const p = S.buildPosition(s, "ETH", {spot: 1700, dvol: 65, avg20: 1800, entry: T("2026-11-27T18:00:00Z"), chain: {}}, 10000, 1.76);
  assert.ok(!p.error && !p.listed && !p.priced && p.up === false);
  assert.ok(p.legs.every(l => l.strike % 20 === 0) && p.legs[0].side === "buy" && p.legs[0].call);
});

test("the weekly cap scales all positions until the total worst case is 25% of capital", () => {
  const mk = (ml, c) => ({coin: "BTC", contracts: c, maxLoss: ml, fees: 1, creditUsd: 10});
  const ps = [mk(2000, 100), mk(2000, 100)];
  const r = S.applyCap(ps, 10000);
  assert.ok(r.scale < 1 && r.total <= 2500 + 1e-9 && ps.every(p => p.capped && p.contracts === 62));
  const ok = [mk(500, 10)];
  assert.strictEqual(S.applyCap(ok, 10000).scale, 1);
});
