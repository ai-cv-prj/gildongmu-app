const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const response = (body = {}, status = 200) => ({ ok: status < 400, status,
  headers: { get: () => "server-request-id" }, json: async () => body });
function harness(fetchResult) {
  const requests = [], events = [], timers = new Map();
  let nextTimer = 0;
  const context = { Blob, FormData, AbortController, URLSearchParams,
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    GDiagnostics: { record(type, fields) { events.push({ type, ...fields }); } },
    fetch(path, options) { requests.push({ path, options }); return fetchResult(requests.length, options); },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/api.js", "utf8"), context);
  return { api: context.GApi, requests, events, timers,
    async fire(delay) {
      await settle();
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected a ${delay}ms timer`);
      timers.delete(entry[0]); entry[1].callback(); await settle();
    } };
}
const jpeg = () => new Blob(["jpeg"], { type: "image/jpeg" });

test("lost frame response retries the identical payload with a fresh request ID", async () => {
  const h = harness(async count => {
    if (count === 1) throw new TypeError("Failed to fetch");
    return response({ frame_id: 7 });
  });
  const pending = h.api.frame("a".repeat(32), 7, 123456, jpeg());
  await h.fire(500);
  assert.deepEqual(await pending, { frame_id: 7 });
  assert.equal(h.requests.length, 2);
  assert.strictEqual(h.requests[0].options.body, h.requests[1].options.body);
  assert.notEqual(h.requests[0].options.headers["X-Request-ID"], h.requests[1].options.headers["X-Request-ID"]);
  assert.deepEqual(h.events.map(event => event.type), ["request_error", "request_retry", "request_recovered"]);
  assert.equal(h.events[0].stage, "request");
  assert.equal(h.events[0].frame_id, 7);
  assert.equal(h.timers.size, 0);
});

test("a broken HTTP 200 JSON body is retried and reported instead of becoming an empty result", async () => {
  const h = harness(async () => ({ ...response(), json: async () => { throw new TypeError("body interrupted"); } }));
  const failed = assert.rejects(h.api.frame("a".repeat(32), 2, 123456, jpeg()), error => {
    assert.equal(error.stage, "response_body");
    assert.equal(error.status, 200);
    assert.equal(h.api.isRetryable(error), true);
    return true;
  });
  await h.fire(500); await h.fire(1000); await failed;
  assert.equal(h.requests.length, 3);
  assert.equal(h.events.filter(event => event.type === "request_error").length, 3);
  assert.equal(h.events[0].server_request_id, "server-request-id");
  assert.equal(h.timers.size, 0);
});

test("the request deadline also bounds a response body that never completes", async () => {
  const h = harness(async count => count === 1
    ? { ...response(), json: () => new Promise(() => {}) } : response({ frame_id: 1 }));
  const pending = h.api.frame("a".repeat(32), 1, 123456, jpeg());
  await h.fire(10000);
  assert.equal(h.requests[0].options.signal.aborted, true);
  await h.fire(500);
  assert.deepEqual(await pending, { frame_id: 1 });
  assert.equal(h.events[0].stage, "timeout");
  assert.equal(h.events[0].error_name, "TimeoutError");
});

test("cancelling a request or its retry delay never submits another frame", async () => {
  for (const inDelay of [false, true]) {
    const controller = new AbortController();
    const h = harness(() => inDelay ? Promise.reject(new TypeError("Failed to fetch")) : new Promise(() => {}));
    const failed = assert.rejects(h.api.frame("a".repeat(32), 1, 123456, jpeg(), controller.signal), error => {
      assert.equal(error.name, "AbortError");
      assert.equal(h.api.isRetryable(error), false);
      return true;
    });
    await settle(); controller.abort(); await failed;
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].options.signal.aborted, !inDelay);
    assert.equal(h.timers.size, 0);
  }
});

test("HTTP validation failures and non-idempotent session creation are never automatically replayed", async () => {
  const validation = harness(async () => response({ detail: "프레임 번호 오류" }, 409));
  await assert.rejects(validation.api.frame("a".repeat(32), 1, 123456, jpeg()), error => {
    assert.equal(error.stage, "http"); assert.equal(error.status, 409);
    assert.equal(validation.api.isRetryable(error), false); return true;
  });
  const start = harness(async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(start.api.start("private device", "private note"), error => error.retryable === true);
  assert.equal(start.requests.length, 1);
  assert.equal(start.timers.size, 0);
  assert.doesNotMatch(JSON.stringify(start.events), /private device|private note/);
});

test("stop and saved-video status recover from transient HTTP errors", async () => {
  for (const method of ["stop", "clips"]) {
    const h = harness(async count => count === 1 ? response({}, 503) : response({ saved: true }));
    const pending = h.api[method]("a".repeat(32));
    await h.fire(500);
    assert.deepEqual(await pending, { saved: true });
    assert.equal(h.requests.length, 2);
  }
});

test("non-JSON HTTP failures retain the HTTP status and response-body failure stage", async () => {
  const h = harness(async () => ({ ...response({}, 422), json: async () => { throw new SyntaxError("Unexpected token <"); } }));
  await assert.rejects(h.api.clips("a".repeat(32)), error => {
    assert.equal(error.status, 422);
    assert.equal(error.stage, "response_body");
    assert.match(error.message, /422/);
    assert.equal(error.retryable, false);
    return true;
  });
  assert.equal(h.requests.length, 1);
  assert.equal(h.events[0].error_name, "SyntaxError");
});

test("arrival failures record the path without bus, coordinate, or accuracy query parameters", async () => {
  const h = harness(async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(h.api.nearbyBusArrival({ busNumber: "private-route", latitude: 37.12345, longitude: 127.56789, accuracyM: 20 }));
  assert.equal(h.events[0].path, "/api/nearby-bus-arrival");
  assert.doesNotMatch(JSON.stringify(h.events), /37\.12345|127\.56789|private-route|accuracy_m/);
});
