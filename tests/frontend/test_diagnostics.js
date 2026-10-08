const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const KEY = "gildongmu-client-events-v1";
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness({ storage = new Map(), online = true, send = null, storageFails = false } = {}) {
  const requests = [], listeners = new Map(), timers = new Map(), intervals = [];
  let nextTimer = 0, sender = send;
  const context = { Blob, AbortController,
    navigator: { onLine: online, userAgent: "test-browser", connection: { type: "wifi", effectiveType: "4g" } },
    performance: { now: () => 1234.5, timeOrigin: 1700000000000 },
    localStorage: {
      getItem(key) { if (storageFails) throw new Error("disabled"); return storage.get(key); },
      setItem(key, value) { if (storageFails) throw new Error("disabled"); storage.set(key, value); },
    },
    document: { visibilityState: "visible", addEventListener(type, callback) { listeners.set(type, callback); } },
    addEventListener(type, callback) { listeners.set(type, callback); },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval(callback) { intervals.push(callback); },
    async fetch(path, options) {
      requests.push({ path, options });
      return sender ? sender(path, options) : { ok: true, json: async () => ({ accepted: JSON.parse(options.body).events.length }) };
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/diagnostics.js", "utf8"), context);
  return { diagnostics: context.GDiagnostics, requests, storage, listeners, context, timers, intervals,
    saved: () => JSON.parse(storage.get(KEY) || "[]"), send(value) { sender = value; } };
}

test("offline errors survive reload and response loss until a batch is explicitly acknowledged", async () => {
  const storage = new Map(), offline = harness({ storage, online: false });
  offline.diagnostics.setContext(() => ({ session_id: "a".repeat(32), paused: true }));
  offline.diagnostics.record("request_error", { request_id: "attempt-1", stage: "request", error_message: "Failed to fetch" });
  await offline.diagnostics.flush();
  assert.equal(offline.requests.length, 0);
  const original = offline.saved()[0];
  assert.equal(original.online, false);
  assert.equal(original.monotonic_clock_ms, 1235);
  assert.equal(original.time_origin_ms, 1700000000000);

  const reloaded = harness({ storage, send: async () => { throw new TypeError("Failed to fetch"); } });
  await settle();
  assert.equal(reloaded.requests.length, 1, "boot retries a previous session's diagnostics");
  assert.equal(reloaded.saved()[0].event_id, original.event_id);
  assert.equal(reloaded.saved().length, 1, "a reporting failure does not log itself recursively");
  reloaded.send(null);
  reloaded.listeners.get("online")(); await settle();
  assert.equal(reloaded.requests.length, 2);
  const submitted = JSON.parse(reloaded.requests[1].options.body).events;
  assert.equal(submitted[0].event_id, original.event_id);
  assert.equal(submitted[0].session_id, "a".repeat(32));
  assert.equal(submitted[1].type, "network_online");
  assert.deepEqual(reloaded.saved(), []);
  assert.equal(reloaded.timers.size, 0);
});

test("the durable ring retains the first failure plus recent events and sends bounded batches", async () => {
  const h = harness({ online: false });
  for (let frame_id = 1; frame_id <= 150; frame_id++) h.diagnostics.record("request_error", { frame_id });
  assert.equal(h.saved().length, 100);
  assert.equal(h.saved()[0].frame_id, 1, "the initial failed request survives a prolonged outage");
  assert.equal(h.saved()[1].frame_id, 52);
  assert.equal(h.saved().at(-1).frame_id, 150);
  assert.equal(new Set(h.saved().map(event => event.event_id)).size, 100);
  h.context.navigator.onLine = true;
  await h.diagnostics.flush();
  assert.equal(JSON.parse(h.requests[0].options.body).events.length, 20);
  assert.equal(h.requests.length, 5, "reconnection drains successive acknowledged batches");
  assert.equal(h.saved().length, 0);
});

test("only an acknowledged batch is removed, preserving new events added during delivery", async () => {
  let resolve;
  const h = harness({ send: () => new Promise(done => { resolve = done; }) });
  h.diagnostics.record("request_error", { request_id: "old" });
  const pending = h.diagnostics.flush();
  await settle();
  h.diagnostics.record("request_error", { request_id: "new" });
  assert.strictEqual(h.diagnostics.flush(), pending, "parallel sends share one pending flush");
  h.send(async () => ({ ok: false }));
  resolve({ ok: true, json: async () => ({ accepted: 1 }) }); await pending;
  assert.deepEqual(h.saved().map(event => event.request_id), ["new"]);
  h.send(async () => ({ ok: true, json: async () => ({ accepted: 0 }) }));
  await h.diagnostics.flush();
  assert.equal(h.saved().length, 1, "HTTP 200 without the expected acknowledgment is not sufficient");
});

test("diagnostics omit request bodies, query values, coordinates and unrecognized context", async () => {
  const h = harness({ online: false });
  h.diagnostics.setContext(() => ({ session_id: "a".repeat(32), screen: "input",
    latitude: 37.12345, longitude: 127.56789, note: "secret note", body: "private body" }));
  h.diagnostics.record("request_error", { path: "/api/nearby-bus-arrival?bus_number=secret-route&latitude=37.12345",
    error_message: "Request https://example.test/api?private=secret failed", user_agent: "ignored override",
    expected_clip_ids: [1, 2, 3, 4, 5, 6], pending_stop_session_id: "invalid-session" });
  const event = h.saved()[0];
  assert.equal(event.path, "/api/nearby-bus-arrival");
  assert.equal(event.error_message, "Request https://example.test/api[redacted] failed");
  assert.equal(event.user_agent, "test-browser");
  assert.equal(event.screen, "input");
  assert.equal(event.pending_stop_session_id, undefined);
  assert.deepEqual(event.expected_clip_ids, [1, 2, 3, 4, 5]);
  assert.doesNotMatch(JSON.stringify(event), /37\.12345|127\.56789|secret|private body/);
});

test("pagehide uses keepalive and unavailable storage does not interrupt diagnostics", async () => {
  const h = harness({ storageFails: true });
  h.diagnostics.record("session_stop", { reason: "user" });
  h.listeners.get("pagehide")(); await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.keepalive, true);
  assert.deepEqual(JSON.parse(h.requests[0].options.body).events.map(event => event.type), ["session_stop", "page_hidden"]);
});

test("오류 버튼 구독과 내보내기 기록은 서버 전달 및 새로고침 후에도 유지된다", async () => {
  const storage = new Map(), h = harness({ storage, online: false });
  const updates = [];
  h.diagnostics.subscribe(summary => updates.push(summary));
  h.diagnostics.record("session_started");
  assert.equal(updates.at(-1).count, 0);
  h.diagnostics.record("request_error", { operation: "frame", error_message: "Failed to fetch" });
  assert.equal(updates.at(-1).count, 1);
  assert.equal(updates.at(-1).pending, 1);
  h.context.navigator.onLine = true;
  await h.diagnostics.flush();
  assert.equal(updates.at(-1).count, 1);
  assert.equal(updates.at(-1).pending, 0);
  assert.equal(h.diagnostics.exportData().events.find(e => e.type === "request_error").delivered, true);
  const reloaded = harness({ storage, online: false });
  assert.equal(reloaded.diagnostics.summary().count, 1);
  assert.equal(reloaded.diagnostics.exportData().events.find(e => e.type === "request_error").error_message, "Failed to fetch");
});

test("기기 기록이 불가능해도 오류 알림과 내보내기는 메모리에서 동작한다", () => {
  const h = harness({ storageFails: true, online: false });
  h.diagnostics.record("clip_preserve_failed", { error_message: "QuotaExceededError" });
  assert.equal(h.diagnostics.summary().count, 1);
  assert.equal(h.diagnostics.summary().storageAvailable, false);
  assert.equal(h.diagnostics.exportData().events[0].type, "clip_preserve_failed");
});
