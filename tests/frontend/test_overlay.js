/**
 * file_path: tests/frontend/test_overlay.js
 *
 * 모바일 장애물 오버레이에 추적 ID와 위험 이벤트 ID가 함께 표시되는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const labels = [];
const rectangles = [];
const context2d = {
  beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {},
  strokeRect(...args) { rectangles.push(args); }, fillRect() {}, clearRect() {}, drawImage() {}, arc() {},
  save() {}, restore() {},
  measureText(text) { return { width: text.length * 8 }; },
  fillText(text) { labels.push(text); },
};
const canvas = { width: 0, height: 0, getContext: () => context2d };
const context = {
  window: {},
  document: { getElementById: () => canvas },
  Image: class {},
};
vm.runInNewContext(fs.readFileSync("frontend/js/overlay.js", "utf8"), context);

let drawState;
context.window.GOverlay.render({
  image_width: 360,
  image_height: 640,
  walking: {
    detections: [{
      xyxy: [.1, .2, .3, .6], display_label: "person", alert_level: "danger",
      track_id: 12, event_id: 34,
    }],
    event: { roi: {}, last_action: "left" },
  },
  traffic: { detections: [] },
  crosswalk: { event: { status: "crossing" } },
  stop_proximity: { status: "candidate", observations: 1,
    required_observations: 3, xyxy: [.1, .2, .5, .8] },
}, state => { drawState = state; });

assert.ok(labels.includes("person · T12 · E34"));
assert.ok(labels.includes("ACTION: left"));
assert.ok(labels.includes("CROSSWALK: crossing"));
assert.ok(labels.includes("정류장 후보 1/3"));
assert.equal(drawState, "drawn");

labels.length = rectangles.length = 0;
context.window.GOverlay.render({
  image_width: 360, image_height: 640,
  walking: { event: { roi: {} } },
  stop_proximity: { status: "nearby", held: true, arrival_recorded: true,
    basis: "left", xyxy: [.1, .2, .5, .8] },
});
assert.ok(labels.includes("좌측 정류장 근접 유지 · 재확인 중"));
// Only the diagnostic banner is outlined; a held stop box must not be drawn.
assert.equal(rectangles.length, 1);
labels.length = 0;
context.window.GOverlay.render({
  image_width: 360, image_height: 640,
  walking: { event: { roi: {} } },
  stop_proximity: { status: "not_detected", arrival_recorded: true },
});
assert.ok(labels.includes("정류장 확인 기록 · 재확인 중"));
console.log("overlay ids: pass");

// Results older than 400ms must not leave a person box on a newer camera frame.
context.performance = { timeOrigin: 1000, now: () => 1000 };
labels.length = rectangles.length = 0;
context.window.GOverlay.render({ image_width: 360, image_height: 640, captured_at_ms: 1500,
  walking: { detections: [{ xyxy: [.1, .2, .3, .6], class_name: "person" }], event: { roi: {} } },
}, state => { drawState = state; });
assert.equal(drawState, "stale");
assert.equal(rectangles.length, 0);
