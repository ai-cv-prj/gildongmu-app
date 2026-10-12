const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const settings = require("./settings");

function harness() {
  let now = 0, current;
  const videoTrack = { readyState: "live" };
  const canvas = { getContext: () => ({}) };
  class Recorder {
    static isTypeSupported() { return true; }
    constructor(stream) {
      current = this; this.stream = stream; this.state = "inactive";
      this.mimeType = "video/webm";
    }
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; }
    finish() {
      this.state = "inactive";
      this.ondataavailable({ data: new Blob(["recorded-video-and-audio"]) });
      this.onstop();
    }
  }
  const context = {
    window: { GConfig: { get: () => settings }, MediaRecorder: Recorder },
    MediaRecorder: Recorder, MediaStream: class { constructor(tracks) { this.tracks = tracks; } },
    Blob, performance: { now: () => now, timeOrigin: 1700000000000 },
    document: { getElementById: id => id === "video"
      ? { srcObject: { getVideoTracks: () => [videoTrack] } } : canvas },
  };
  vm.runInNewContext(fs.readFileSync("frontend/js/recorder.js", "utf8"), context);
  return { recorder: context.window.GRecorder, videoTrack,
    at: value => { now = value; }, current: () => current };
}

for (const elapsed of [30001, 89999, 90000, 90001]) {
  for (const browserStopped of [false, true]) {
    test(`${browserStopped ? "browser" : "explicit"} stop preserves ${elapsed}ms recording up to the 90 second limit`, async () => {
      const h = harness(), ended = [];
      const start = h.recorder.startRaw(null, error => ended.push(error));
      h.at(elapsed);
      const pending = browserStopped ? null : h.recorder.stopRaw();
      h.current().finish();
      const saved = await (pending || h.recorder.stopRaw());
      assert.equal(saved.started_at_ms, start);
      assert.equal(saved.ended_at_ms - start, Math.min(elapsed, 90000));
      assert.ok(saved.blob.size > 0);
      assert.equal(h.recorder.active(), false);
      assert.deepEqual(ended, [null]);
    });
  }
}

test("delayed encoder finalization retains the requested stop time and the guidance audio track", async () => {
  const h = harness(), guidance = { readyState: "live" }, endedAudio = { readyState: "ended" };
  const start = h.recorder.startRaw(null, null, { getAudioTracks: () => [guidance, endedAudio] });
  assert.equal(h.current().stream.tracks.length, 2);
  assert.strictEqual(h.current().stream.tracks[0], h.videoTrack);
  assert.strictEqual(h.current().stream.tracks[1], guidance);
  h.at(89500);
  const pending = h.recorder.stopRaw();
  h.at(94000);
  h.current().finish();
  const saved = await pending;
  assert.equal(saved.ended_at_ms - start, 89500);
  assert.equal(h.videoTrack.readyState, "live");
  assert.equal(guidance.readyState, "live");
});
