const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness({ deferPermission = false, android = false, applyBehavior = async () => {} } = {}) {
  const listeners = new Map();
  const cameraSettings = { exposureMode: "continuous", exposureTime: 30 };
  let draws = 0, applies = 0;
  const track = { getCapabilities: () => ({ exposureMode: ["continuous", "manual"],
      exposureTime: { min: 1, max: 1000, step: 1 } }),
    getSettings: () => ({ ...cameraSettings }), getConstraints: () => ({}),
    async applyConstraints(value) {
      await applyBehavior(++applies);
      for (const part of value.advanced || []) Object.assign(cameraSettings, part);
    }, readyState: "live", label: "fake camera", enabled: true,
    stop() { this.readyState = "ended"; }, addEventListener() {} };
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
  let grant;
  const video = { readyState: 0, videoWidth: 640, videoHeight: 480,
    srcObject: null, play: async () => {},
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); } };
  const context = { setTimeout, clearTimeout, navigator: { userAgent: android ? "Android" : "iPhone", mediaDevices: {
    getUserMedia: () => deferPermission ? new Promise(resolve => { grant = resolve; }) : Promise.resolve(stream),
  } }, document: { getElementById: () => video,
    createElement: () => ({ getContext: () => ({ drawImage() { draws++; } }), toBlob(fn) { fn({ size: 1 }); } }) },
    window: { GConfig: { get: () => ({ camera: { width: 640, height: 480, facing_mode: "environment", bus_led_exposure_enabled: true } }) } } };
  vm.runInNewContext(fs.readFileSync("frontend/js/camera-exposure.js", "utf8"), context);
  vm.runInNewContext(fs.readFileSync("frontend/js/camera.js", "utf8"), context);
  return { camera: context.window.GCamera, track, video, listeners, draws: () => draws, grant: () => grant(stream) };
}

test("ending during camera permission stops a subsequently granted stream", async () => {
  const app = harness({ deferPermission: true });
  const starting = app.camera.start();
  app.camera.stop();
  app.grant();
  await assert.rejects(starting, { name: "AbortError" });
  assert.equal(app.track.readyState, "ended");
  assert.equal(app.video.srcObject, null);
});

test("ending during metadata wait releases pending camera start and listeners", async () => {
  const app = harness();
  const starting = app.camera.start();
  await new Promise(setImmediate);
  assert.equal(app.listeners.has("loadedmetadata"), true);
  app.camera.stop();
  await assert.rejects(starting, { name: "AbortError" });
  assert.equal(app.listeners.size, 0);
  assert.equal(app.track.readyState, "ended");
});

test("pause and resume disable capture tracks without replacing the camera stream", async () => {
  const app = harness();
  const starting = app.camera.start();
  await new Promise(setImmediate);
  app.listeners.get("loadedmetadata")();
  const result = await starting;
  assert.equal(result.width, 640);
  app.camera.pause();
  assert.equal(app.track.enabled, false);
  assert.equal(app.camera.active(), true);
  app.camera.resume();
  assert.equal(app.track.enabled, true);
  app.camera.stop();
});

async function openCamera(app) {
  const starting = app.camera.start();
  await new Promise(setImmediate);
  app.listeners.get("loadedmetadata")();
  await starting;
}

test("capture waits for bus exposure changes before sampling the shared camera", async () => {
  let release;
  const app = harness({ android: true, applyBehavior: call => call === 1
    ? new Promise(resolve => { release = resolve; }) : Promise.resolve() });
  await openCamera(app);
  const applying = app.camera.setBusMode(true);
  await new Promise(setImmediate);
  const capture = app.camera.capture(640, .72);
  await new Promise(setImmediate);
  assert.equal(app.draws(), 0);
  release();
  await applying;
  assert.equal((await capture).size, 1);
  assert.equal(app.draws(), 1);
  assert.equal(app.camera.exposureStatus().status, "applied");
  await app.camera.setBusMode(false);
  assert.equal(app.track.getSettings().exposureMode, "continuous");
  app.camera.stop();
});

test("a failed restoration retires the real track and notifies camera loss", async () => {
  const app = harness({ android: true, applyBehavior: async call => {
    if (call >= 3) throw new Error("camera rejected restore");
  } });
  let ended = 0;
  const reports = [];
  app.camera.setOnEnded(() => ended++);
  app.camera.setOnExposureChange(report => reports.push(report));
  await openCamera(app);
  await app.camera.setBusMode(true);
  await app.camera.setBusMode(false);
  assert.equal(app.track.readyState, "ended");
  assert.equal(app.camera.active(), false);
  assert.equal(ended, 1);
  assert.equal(reports.at(-1).status, "restore_failed");
  assert.equal(await app.camera.capture(640, .72), null);
});

test("stop releases a capture waiting on native camera settings", async () => {
  let release;
  const app = harness({ android: true, applyBehavior: () => new Promise(resolve => { release = resolve; }) });
  await openCamera(app);
  const applying = app.camera.setBusMode(true);
  await new Promise(setImmediate);
  const capturing = app.camera.capture(640, .72);
  app.camera.stop();
  assert.equal(await capturing, null);
  await applying;
  assert.equal(app.draws(), 0);
  release();
  await new Promise(setImmediate);
  assert.equal(app.camera.active(), false);
});
