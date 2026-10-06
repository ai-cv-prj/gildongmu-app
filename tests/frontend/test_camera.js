const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness({ deferPermission = false } = {}) {
  const listeners = new Map();
  const track = { readyState: "live", label: "fake camera", enabled: true,
    stop() { this.readyState = "ended"; }, addEventListener() {} };
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
  let grant;
  const video = { readyState: 0, videoWidth: 640, videoHeight: 480,
    srcObject: null, play: async () => {},
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); } };
  const context = { setTimeout, clearTimeout, navigator: { mediaDevices: {
    getUserMedia: () => deferPermission ? new Promise(resolve => { grant = resolve; }) : Promise.resolve(stream),
  } }, document: { getElementById: () => video,
    createElement: () => ({ getContext: () => ({}) }) },
    window: { GConfig: { get: () => ({ camera: { width: 640, height: 480, facing_mode: "environment" } }) } } };
  vm.runInNewContext(fs.readFileSync("frontend/js/camera.js", "utf8"), context);
  return { camera: context.window.GCamera, track, video, listeners, grant: () => grant(stream) };
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
