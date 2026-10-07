const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(setImmediate);

function harness({ settings = {}, constraints, capabilities, behavior, options = {} } = {}) {
  const context = { window: {}, setTimeout, clearTimeout };
  vm.runInNewContext(fs.readFileSync("frontend/js/camera-exposure.js", "utf8"), context);
  const current = { exposureMode: "continuous", exposureTime: 50,
    width: 1280, height: 720, frameRate: 30, zoom: 1.75, ...settings };
  let activeConstraints = clone(constraints || { width: { ideal: 1280 }, height: { ideal: 720 },
    frameRate: { ideal: 30 }, facingMode: "environment",
    advanced: [{ zoom: 1.75, focusMode: "continuous" }, { exposureMode: "continuous" }] });
  const original = clone(activeConstraints);
  const calls = [], changes = [], fatals = [];
  const track = {
    readyState: "live",
    getSettings: () => clone(current),
    getConstraints: () => clone(activeConstraints),
    getCapabilities: () => capabilities === undefined ? {
      exposureMode: ["continuous", "manual"], exposureTime: { min: 1, max: 10000, step: 1 },
    } : clone(capabilities),
    async applyConstraints(next) {
      const request = clone(next);
      calls.push(request);
      const effect = behavior ? await behavior(request, calls.length) : {};
      activeConstraints = request;
      for (const values of [request, ...(request.advanced || [])]) {
        if (values.exposureMode !== undefined && !effect?.ignoreMode) current.exposureMode = values.exposureMode;
        if (values.exposureTime !== undefined && !effect?.ignoreTime && current.exposureMode === "manual") {
          current.exposureTime = values.exposureTime;
        }
      }
    },
  };
  const controller = context.window.GCameraExposure.create(track, {
    android: true, exposureTimeUs: 16667, verifyMs: 0, timeoutMs: 1000,
    onChange: value => changes.push(clone(value)), onFatal: reason => fatals.push(reason), ...options,
  });
  return { controller, track, current, original, calls, changes, fatals };
}

const exposureValues = request => Object.assign({}, request, ...(request.advanced || []));

test("initial camera and repeated disabled mode leave the default stream untouched", async () => {
  const app = harness();
  assert.equal(app.controller.snapshot().status, "default");
  assert.equal(app.controller.snapshot().actual_us, null,
    "automatic exposure time readback is not a verified shutter measurement");
  await app.controller.setBusMode(false);
  await app.controller.settled();
  assert.equal(app.calls.length, 0);
  assert.equal(app.current.exposureMode, "continuous");
  const snapshot = app.controller.snapshot();
  snapshot.settings.width = 10;
  assert.equal(app.controller.snapshot().settings.width, 1280);
  app.controller.dispose();
});

test("bus mode selects manual exposure then 1/60 second in 100 microsecond units", async () => {
  const app = harness();
  const result = await app.controller.setBusMode(true);
  assert.equal(app.calls.length, 2);
  assert.equal(exposureValues(app.calls[0]).exposureMode, "manual");
  assert.equal(exposureValues(app.calls[0]).exposureTime, undefined);
  assert.equal(exposureValues(app.calls[1]).exposureTime, 167);
  assert.equal(result.status, "applied");
  assert.equal(result.requested, true);
  assert.equal(result.target_us, 16667);
  assert.equal(result.actual_us, 16700);
  assert.equal(result.baseline_settings.exposureMode, "continuous");
  assert.equal(result.capabilities.exposureTime.step, 1);
  assert.equal(app.fatals.length, 0);
  for (const request of app.calls) {
    assert.deepEqual(request.width, app.original.width);
    assert.deepEqual(request.height, app.original.height);
    assert.deepEqual(request.frameRate, app.original.frameRate);
    assert.equal(request.facingMode, "environment");
    assert.deepEqual(request.advanced[0], { zoom: 1.75, focusMode: "continuous" });
    assert.equal(request.advanced.some(values => values.exposureMode === "continuous"), false);
  }
  await app.controller.setBusMode(true);
  assert.equal(app.calls.length, 2, "repeated enable must not restart native camera controls");
  app.controller.dispose();
});

test("leaving bus mode restores automatic exposure and the original constraints", async () => {
  const app = harness();
  await app.controller.setBusMode(true);
  const enabledCalls = app.calls.length;
  const result = await app.controller.setBusMode(false);
  assert.equal(result.status, "default");
  assert.equal(result.requested, false);
  assert.equal(app.current.exposureMode, "continuous");
  assert.deepEqual(app.calls.at(-1), app.original);
  for (const request of app.calls.slice(enabledCalls)) {
    assert.equal(exposureValues(request).exposureTime, undefined,
      "automatic exposure must not be restored by fixing the old shutter time");
  }
  const count = app.calls.length;
  await app.controller.setBusMode(false);
  assert.equal(app.calls.length, count);
  app.controller.dispose();
});

test("unsupported devices and un-restorable exposure modes do not modify the camera", async t => {
  const cases = [
    { name: "non Android", options: { android: false } },
    { name: "feature disabled", options: { enabled: false } },
    { name: "no manual mode", capabilities: { exposureMode: ["continuous"] } },
    { name: "missing exposure capabilities", capabilities: {} },
    { name: "requested exposure outside range", capabilities: {
      exposureMode: ["continuous", "manual"], exposureTime: { min: 1, max: 100, step: 1 },
    } },
    { name: "unknown prior mode", settings: { exposureMode: "single-shot" } },
    { name: "unknown prior manual time", settings: { exposureMode: "manual", exposureTime: undefined } },
  ];
  for (const scenario of cases) await t.test(scenario.name, async () => {
    const app = harness(scenario);
    const result = await app.controller.setBusMode(true);
    assert.equal(result.status, "unsupported");
    assert.equal(app.calls.length, 0);
    assert.equal(app.fatals.length, 0);
    app.controller.dispose();
  });
  await t.test("missing getCapabilities API", async () => {
    const app = harness();
    delete app.track.getCapabilities;
    assert.equal((await app.controller.setBusMode(true)).status, "unsupported");
    assert.equal(app.calls.length, 0);
    app.controller.dispose();
  });
});

test("a driver silently ignoring mode or time is rolled back and never reported applied", async t => {
  for (const ignored of ["ignoreMode", "ignoreTime"]) await t.test(ignored, async () => {
    const app = harness({ behavior: () => ({ [ignored]: true }) });
    const result = await app.controller.setBusMode(true);
    assert.equal(result.status, "failed");
    assert.equal(app.current.exposureMode, "continuous");
    assert.deepEqual(app.calls.at(-1), app.original);
    assert.equal(app.changes.some(change => change.status === "applied"), false);
    assert.equal(result.failed_settings.exposureMode, ignored === "ignoreMode" ? "continuous" : "manual");
    assert.equal(result.failed_settings.exposureTime, 50,
      "failed readback must survive restoration of the automatic mode");
    assert.equal(result.settings.exposureMode, "continuous");
    assert.equal(app.fatals.length, 0);
    app.controller.dispose();
  });
});

test("unsupported exposure reports distinguish missing controls from an out-of-range target", async t => {
  const cases = [
    { capabilities: { exposureMode: ["continuous"] }, reason: "manual_mode_unavailable" },
    { capabilities: { exposureMode: ["continuous", "manual"] }, reason: "exposure_time_unavailable" },
    { capabilities: { exposureMode: ["continuous", "manual"], exposureTime: { min: 1, max: 100, step: 1 } },
      reason: "exposure_time_out_of_range" },
  ];
  for (const scenario of cases) await t.test(scenario.reason, async () => {
    const app = harness(scenario);
    const result = await app.controller.setBusMode(true);
    assert.equal(result.status, "unsupported");
    assert.equal(result.reason, scenario.reason);
    assert.deepEqual(clone(result.capabilities), scenario.capabilities);
    assert.equal(result.baseline_settings.exposureMode, "continuous");
    assert.equal(app.calls.length, 0);
    app.controller.dispose();
  });
});

test("a changed shutter is reported once before capture and the original mode still restores", async t => {
  for (const drift of [{ exposureMode: "continuous" }, { exposureTime: 30 }, { exposureTime: undefined }]) {
    await t.test(JSON.stringify(drift), async () => {
      const app = harness();
      await app.controller.setBusMode(true);
      const nativeCalls = app.calls.length;
      assert.equal(app.controller.check().status, "applied");
      Object.assign(app.current, drift);
      const result = app.controller.check();
      assert.equal(result.status, "changed");
      assert.equal(result.reason, "settings_changed");
      assert.equal(result.requested_time_us, 16700);
      assert.deepEqual(result.failed_settings, result.settings);
      for (let i = 0; i < 3; i++) app.controller.check();
      assert.equal(app.changes.filter(change => change.status === "changed").length, 1);
      assert.equal(app.calls.length, nativeCalls, "drift monitoring must not reconfigure a streaming camera");
      await app.controller.setBusMode(false);
      assert.equal(app.controller.snapshot().status, "default");
      assert.equal(app.current.exposureMode, "continuous");
      assert.deepEqual(app.calls.at(-1), app.original);
      app.controller.dispose();
    });
  }
});

test("a rejected shutter request rolls back the successfully selected manual mode", async () => {
  const app = harness({ behavior: (_, index) => {
    if (index === 2) throw Object.assign(new Error("driver rejected shutter"), { name: "OverconstrainedError" });
  } });
  const result = await app.controller.setBusMode(true);
  assert.equal(result.status, "failed");
  assert.match(result.reason, /driver rejected shutter/);
  assert.equal(app.current.exposureMode, "continuous");
  assert.deepEqual(app.calls.at(-1), app.original);
  assert.equal(app.changes.some(change => change.status === "applied"), false);
  assert.equal(app.fatals.length, 0);
  app.controller.dispose();
});

test("a preexisting manual shutter setting is restored after bus mode", async () => {
  const app = harness({ settings: { exposureMode: "manual", exposureTime: 240 },
    constraints: { width: { ideal: 1280 }, frameRate: { ideal: 24 },
      advanced: [{ zoom: 2 }, { exposureMode: "manual", exposureTime: 240 }] } });
  await app.controller.setBusMode(true);
  assert.equal(app.current.exposureTime, 167);
  const result = await app.controller.setBusMode(false);
  assert.equal(result.status, "default");
  assert.equal(app.current.exposureMode, "manual");
  assert.equal(app.current.exposureTime, 240);
  assert.deepEqual(app.calls.at(-1), app.original);
  assert.equal(app.fatals.length, 0);
  app.controller.dispose();
});

test("disabling while mode application is pending restores the camera without publishing applied", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const app = harness({ behavior: (_, index) => index === 1 ? pending : undefined });
  const enabling = app.controller.setBusMode(true);
  await tick();
  assert.equal(app.calls.length, 1);
  const disabling = app.controller.setBusMode(false);
  release();
  await Promise.all([enabling, disabling, app.controller.settled()]);
  assert.equal(app.controller.snapshot().status, "default");
  assert.equal(app.current.exposureMode, "continuous");
  assert.equal(app.calls.some(request => exposureValues(request).exposureTime !== undefined), false);
  assert.equal(app.changes.some(change => change.status === "applied"), false);
  assert.deepEqual(app.calls.at(-1), app.original);
  app.controller.dispose();
});

test("disposing a pending old track prevents later requests and stale status updates", async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const old = harness({ behavior: () => pending });
  const enabling = old.controller.setBusMode(true);
  await tick();
  old.controller.dispose();
  old.track.readyState = "ended";
  const count = old.changes.length;
  await enabling;
  const fresh = harness();
  await fresh.controller.setBusMode(true);
  release();
  await tick();
  await old.controller.setBusMode(true);
  assert.equal(old.calls.length, 1);
  assert.equal(old.changes.length, count);
  assert.equal(old.fatals.length, 0);
  assert.equal(fresh.controller.snapshot().status, "applied");
  assert.equal(fresh.current.exposureTime, 167);
  fresh.controller.dispose();
});

test("a timed-out native operation requests track retirement instead of racing restoration", async () => {
  const app = harness({ behavior: () => new Promise(() => {}), options: { timeoutMs: 15 } });
  const result = await app.controller.setBusMode(true);
  assert.equal(result.status, "restore_failed");
  assert.equal(app.fatals.length, 1);
  assert.match(app.fatals[0], /시간 초과/);
  assert.equal(app.calls.length, 1);
  assert.equal(app.changes.some(change => change.status === "applied"), false);
  await app.controller.setBusMode(false);
  await app.controller.setBusMode(true);
  assert.equal(app.calls.length, 1, "timed out controller must be retired");
  app.controller.dispose();
});
