const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness() {
  let clock = 100000, serial = 0;
  const values = new Map(), timers = new Map();
  const storage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const context = { URL, Headers, Blob, FormData, AbortController, console, Date,
    performance: { now: () => clock }, navigator: { onLine: true },
    document: { visibilityState: "visible" }, location: { href: "https://phone.test/", origin: "https://phone.test" },
    crypto: { randomUUID: () => `request-${++serial}-12345678` }, localStorage: storage,
    fetch: async () => { throw new TypeError("Failed to fetch"); },
    setTimeout(callback, delay) { timers.set(++serial, { callback, delay }); return serial; },
    clearTimeout: key => timers.delete(key), setInterval() {}, addEventListener() {} };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/fetch-diagnostics.js", "utf8"), context);
  const create = options => context.GFetchDiagnostics.create({ storage, now: () => clock,
    monotonic: () => clock, ...options });
  return { context, create, storage, timers, advance(ms) { clock += ms; } };
}
const response = (body = {}, { ok = true, status = 200, source = "member-a" } = {}) => ({
  ok, status, headers: new Headers(source ? { "X-Hub-Source-ID": source } : {}), json: async () => body,
});

test("reload and ended-session delivery retain history and clear only acknowledged reports", async () => {
  const h = harness();
  const first = h.create({ transport: async () => { throw new TypeError("Failed to fetch"); } });
  await assert.rejects(first.fetch("/api/sessions/old-session/clips/2"));
  const sent = [];
  const restored = h.create({ transport: async (path, options) => {
    sent.push({ path, body: JSON.parse(options.body) });
    return response({ saved_ids: sent[0].body.records.map(record => record.event_id) });
  } });
  assert.equal(restored.summary().pending, 1);
  await restored.flush();
  assert.equal(sent[0].path, "/api/client-errors");
  assert.equal(sent[0].body.records[0].clip_id, 2);
  assert.equal(sent[0].body.records[0].session_id, "old-session");
  assert.equal(restored.summary().pending, 0);
  assert.equal(restored.exportData().records[0].delivered, true);
  assert.equal(restored.exportData().records[0].source_id, "member-a");
  assert.equal(h.create().summary().pending, 0);
});

test("diagnostic delivery failures do not recurse or lose pending errors", async () => {
  const h = harness(); let deliveries = 0;
  const diagnostics = h.create({ transport: async path => {
    if (path === "/api/client-errors") deliveries++;
    throw new TypeError("Failed to fetch");
  } });
  await assert.rejects(diagnostics.fetch("/api/sessions/stop", { method: "POST", body: JSON.stringify({ session_id: "old-session" }) }));
  await diagnostics.flush(); await diagnostics.flush();
  assert.equal(deliveries, 2);
  assert.equal(diagnostics.summary().count, 1);
  assert.equal(diagnostics.summary().pending, 1);
  assert.equal(diagnostics.exportData().records[0].session_id, "old-session");
});

test("intentional aborts are not classified as fetch failures", async () => {
  const h = harness();
  const aborted = h.create({ transport: async () => { throw Object.assign(new Error("cancelled"), { name: "AbortError" }); } });
  await assert.rejects(aborted.fetch("/api/health"));
  assert.equal(aborted.summary().count, 0);
});

test("private query data is excluded and storage failure preserves downloadable memory history", async () => {
  const h = harness();
  const diagnostics = h.create({ storage: { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } },
    transport: async () => { throw new TypeError("Failed to fetch"); } });
  await assert.rejects(diagnostics.fetch("/api/nearby-bus-arrival?latitude=37&longitude=127&token=private"));
  assert.equal(diagnostics.summary().storageAvailable, false);
  assert.equal(diagnostics.exportData().records[0].path, "/api/nearby-bus-arrival");
  assert.equal(JSON.stringify(diagnostics.exportData()).includes("private"), false);
});

test("history is bounded and unknown server acknowledgements do not discard pending data", async () => {
  const h = harness();
  const diagnostics = h.create({ transport: async path => {
    if (path === "/api/client-errors") return response({ saved_ids: ["unrelated-id"] });
    throw new TypeError("Failed to fetch");
  } });
  for (let i = 0; i < 102; i++) await assert.rejects(diagnostics.fetch("/api/config"));
  await diagnostics.flush();
  assert.equal(diagnostics.summary().count, 100);
  assert.equal(diagnostics.summary().pending, 100);
  assert.equal(diagnostics.exportData().dropped_records, 2);
});

test("overlapping flush calls share one delivery", async () => {
  const h = harness(); let release, calls = 0;
  const diagnostics = h.create({ transport: async (path, options) => {
    if (path !== "/api/client-errors") throw new TypeError("Failed to fetch");
    calls++;
    const ids = JSON.parse(options.body).records.map(record => record.event_id);
    return new Promise(resolve => { release = () => resolve(response({ saved_ids: ids })); });
  } });
  await assert.rejects(diagnostics.fetch("/api/config"));
  const first = diagnostics.flush(), second = diagnostics.flush();
  assert.equal(first, second);
  release(); await first;
  assert.equal(calls, 1);
  assert.equal(diagnostics.summary().pending, 0);
});

test("offline errors can be downloaded without contacting a server", async () => {
  const h = harness(); let exported, clicked = false, calls = 0;
  h.context.navigator.onLine = false;
  h.context.URL = class extends URL {
    static createObjectURL(blob) { exported = blob; return "blob:error-log"; }
    static revokeObjectURL() {}
  };
  const link = { click() { clicked = true; }, remove() {} };
  h.context.document.createElement = () => link;
  h.context.document.body = { appendChild() {} };
  const diagnostics = h.create({ transport: async () => { calls++; throw new TypeError("Failed to fetch"); } });
  await assert.rejects(diagnostics.fetch("/api/config"));
  await diagnostics.flush();
  diagnostics.download();
  assert.equal(calls, 1);
  assert.equal(clicked, true);
  assert.match(link.download, /^fetch-errors-\d+\.json$/);
  const data = JSON.parse(await exported.text());
  assert.equal(data.records[0].message, "Failed to fetch");
  assert.equal(data.records[0].delivered, false);
});
