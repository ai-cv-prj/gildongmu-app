const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

module.exports = (async () => {
  let now = 1000, current = null, view = null, calls = [], version = 1;
  let server = { status: "awaiting_stop", arrival_event_id: 1, revision: version, bus_number: null };
  const coordinator = {
    PRIORITY: { emergency: 0, boarding: 6 },
    request(request) {
      if (current && request.priority >= current.priority) return false;
      const old = current;
      current = request;
      old?.onCancel?.();
      return true;
    },
    clear(source) {
      if (current?.source !== source) return;
      const old = current;
      current = null;
      old.onCancel?.();
    },
  };
  const api = { async boarding(id, action, event, number) {
    calls.push({ id, action, event, number });
    if (action === "stop_announced") server.status = "pending";
    if (action === "cancel") server.status = "cancelled";
    if (action === "submit") { server.status = "submitted"; server.bus_number = number; }
    if (action === "reopen") server.status = "awaiting_stop";
    server.revision = ++version;
    return { ...server };
  } };
  const context = { window: {}, performance: { now: () => now } };
  vm.runInNewContext(fs.readFileSync("frontend/js/boarding.js", "utf8"), context);
  const guide = context.window.GBoarding.create({ api, coordinator, now: () => now,
    onChange: next => { view = next; } });
  const accept = (state = server, extra = {}) => guide.accept({ session_id: "session", boarding: { ...state },
    stop_proximity: { nearby: true }, ...extra }, now);
  const finish = () => { const old = current; current = null; old?.onComplete?.(); };
  const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
  guide.start("session");
  accept();
  assert.equal(current.text, "멈추세요.");
  assert.equal(calls.length, 0);
  assert.equal(view.status, "awaiting_stop");
  finish();
  await flush();
  assert.equal(calls[0].action, "stop_announced");
  assert.equal(view.status, "pending");
  guide.tick();
  assert.equal(current.text, context.window.GBoarding.PROMPT);

  // An urgent hazard interrupts the question; only the pending question can resume.
  coordinator.request({ source: "walking", priority: 0, text: "멈추세요." });
  guide.tick();
  assert.equal(current.source, "walking");
  finish();
  guide.tick();
  assert.equal(current.source, "boarding");
  finish();
  guide.tick();
  assert.equal(current, null);

  // Cancellation survives missing detections and older in-flight frame responses.
  const oldPending = { ...server };
  assert.equal(await guide.cancel(), true);
  accept(oldPending, { stop_proximity: { nearby: false } });
  assert.equal(view.status, "cancelled");
  accept();
  guide.tick();
  assert.equal(current, null);
  assert.equal(await guide.reopen(), true);
  guide.tick();
  assert.equal(current.text, "멈추세요.");
  finish(); await flush();
  assert.equal(view.status, "pending");
  assert.equal(await guide.submit("N26"), true);
  assert.equal(view.status, "submitted");
  assert.equal(view.bus_number, "N26");
  guide.stop();
  assert.equal(view, null);
  accept();
  assert.equal(view, null);

  // A crossing and stale camera results cannot initiate a new arrival stop.
  server = { status: "awaiting_stop", arrival_event_id: 1, revision: ++version };
  guide.start("session");
  accept(server, { crosswalk: { event: { crossing_active: true } } });
  assert.equal(current, null);
  guide.accept({ session_id: "session", boarding: server, stop_proximity: { nearby: true } }, now - 1500);
  assert.equal(current, null);
  guide.stop();
  console.log("boarding: pass");
})();
