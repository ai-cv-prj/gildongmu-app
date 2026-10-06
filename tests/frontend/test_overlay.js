/**
 * file_path: tests/frontend/test_overlay.js
 *
 * 모바일 장애물 오버레이에 추적 ID와 위험 이벤트 ID가 함께 표시되는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const labels = [];
const strokeColors = [];
const rectangles = [];
const dashedLines = [];
let currentPoint = null;
let dashPattern = [];
const filledRects = [];
const context2d = {
  beginPath() { currentPoint = null; }, moveTo(...args) { currentPoint = args; },
  lineTo(...args) { if (dashPattern.length) dashedLines.push([currentPoint, args]); currentPoint = args; },
  closePath() {}, fill() {}, stroke() {}, setLineDash(value) { dashPattern = value; },
  strokeRect(...args) { strokeColors.push(this.strokeStyle); rectangles.push(args); },
  fillRect(...args) { filledRects.push({ color: this.fillStyle, args }); },
  clearRect() {}, drawImage() {}, arc() {},
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
    event: { roi: { immediate_polygon: [[.02, .70], [.98, .70], [.98, 1], [.02, 1]] },
      last_action: "left", voice_action: "right", stationarity: { status: "stationary" } },
  },
  traffic: { detections: [] },
  crosswalk: { event: { status: "crossing" } },
  walking_surface: { event: { status: "inside",
    roi: { left: .46, right: .54, top: .88, bottom: .96 } } },
  stop_proximity: { status: "candidate", observations: 1,
    required_observations: 3, xyxy: [.1, .2, .5, .8] },
}, state => { drawState = state; });

assert.ok(labels.includes("person | T12 · E34"));
assert.ok(labels.includes("ACTION: left"));
assert.ok(labels.includes("VOICE: right"));
assert.ok(labels.includes("MOTION: stationary"));
assert.ok(labels.includes("CROSSWALK: crossing"));
assert.ok(labels.includes("WALKWAY: inside"));
assert.ok(strokeColors.includes("#50e65a"));
assert.ok(labels.includes("정류장 후보 1/3"));
assert.deepEqual(dashedLines.at(-1), [[7.2, 544], [352.8, 544]]);
const stopBanner = filledRects.find(rect => rect.color === "#08131feb");
assert.ok(stopBanner.args[1] >= 8 + 2 * (Math.max(26, 640 / 24) + 8),
  "정류장 배지는 횡단보도·보행로 배지 아래에 놓인다");
assert.equal(drawState, "drawn");

labels.length = 0;
context.window.GOverlay.render({
  image_width: 360, image_height: 640,
  walking: { event: { roi: {}, last_action: "stop", voice_action: null } },
});
assert.ok(labels.includes("VOICE: muted"));

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
assert.ok(labels.includes("정류장 도착 기록 있음 ·"));
assert.ok(labels.includes("현재 화면에서 미검출"));
labels.length = 0;
context.window.GOverlay.render({
  image_width: 360, image_height: 640,
  walking: { event: { roi: {} } },
  stop_proximity: { status: "candidate", arrival_recorded: true,
    observations: 1, required_observations: 3 },
});
assert.ok(labels.includes("정류장 후보 재확인 중"));
console.log("overlay ids: pass");

// Results older than 400ms must not leave a person box on a newer camera frame.
context.performance = { timeOrigin: 1000, now: () => 1000 };
labels.length = rectangles.length = 0;
context.window.GOverlay.render({ image_width: 360, image_height: 640, captured_at_ms: 1500,
  walking: { detections: [{ xyxy: [.1, .2, .3, .6], class_name: "person" }], event: { roi: {} } },
}, state => { drawState = state; });
assert.equal(drawState, "stale");
assert.equal(rectangles.length, 0);

// Independent OCR may finish on the next frame, but its own capture must still be fresh.
function busFrame(capturedAt) {
  labels.length = 0;
  context.window.GOverlay.render({ image_width: 360, image_height: 640,
    frame_id: 2, captured_at_ms: 1950,
    bus: { frame_id: 1, captured_at_ms: capturedAt,
      detections: [{ box: { x1: .1, y1: .2, x2: .3, y2: .4 }, extra: { text: "143" } }] },
  });
}
busFrame(1700);
assert.ok(labels.includes("143"));
busFrame(1500);
assert.ok(!labels.includes("143"));
busFrame(2100);
assert.ok(!labels.includes("143"));

labels.length = rectangles.length = 0;
context.window.GOverlay.render({ image_width: 360, image_height: 640, captured_at_ms: 1950,
  walking: { detections: [{ xyxy: [.1, .2, .3, .6], class_name: "person" }],
    event: { enabled: false, last_action: "stop", voice_action: "stop" } },
  bus: { captured_at_ms: 1950, detections: [{ box: { x1: .1, y1: .2, x2: .3, y2: .4 }, extra: { text: "143" } }] },
});
assert.ok(labels.includes("143"));
assert.ok(!labels.some(label => /person|ACTION|VOICE/.test(label)));
