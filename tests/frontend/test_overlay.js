/**
 * file_path: tests/frontend/test_overlay.js
 *
 * 모바일 장애물 오버레이에 추적 ID와 위험 이벤트 ID가 함께 표시되는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const labels = [];
const textColors = [];
const strokeColors = [];
const rectangles = [];
const dashedLines = [];
let currentPoint = null;
let dashPattern = [];
let drawnImages = 0;
const filledRects = [];
const context2d = {
  beginPath() { currentPoint = null; }, moveTo(...args) { currentPoint = args; },
  lineTo(...args) { if (dashPattern.length) dashedLines.push([currentPoint, args]); currentPoint = args; },
  closePath() {}, fill() {}, stroke() {}, setLineDash(value) { dashPattern = value; },
  strokeRect(...args) { strokeColors.push(this.strokeStyle); rectangles.push(args); },
  fillRect(...args) { filledRects.push({ color: this.fillStyle, args }); },
  clearRect() {}, drawImage() { drawnImages++; }, arc() {},
  save() {}, restore() {},
  measureText(text) { return { width: text.length * 8 }; },
  fillText(text) { labels.push(text); textColors.push({ text, color: this.fillStyle }); },
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
      last_action: "left", voice_action: "right", voice_playback_action: "right",
      stationarity: { status: "stationary" } },
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
assert.equal(dashedLines.length, 2);
assert.ok(Math.abs(dashedLines[0][0][0] - 126) < 1e-6);
assert.ok(Math.abs(dashedLines[0][1][0] - 126) < 1e-6);
assert.ok(Math.abs(dashedLines[1][0][0] - 234) < 1e-6);
assert.ok(Math.abs(dashedLines[1][1][0] - 234) < 1e-6);
assert.equal(dashedLines[0][0][1], 0);
assert.equal(dashedLines[0][1][1], 640);
const stopBanner = filledRects.find(rect => rect.color === "#08131feb");
assert.ok(stopBanner.args[1] >= 8 + 2 * (Math.max(26, 640 / 24) + 8),
  "정류장 배지는 횡단보도·보행로 배지 아래에 놓인다");
assert.equal(drawState, "drawn");

labels.length = 0;
context.window.GOverlay.render({
  image_width: 360, image_height: 640,
  walking: { event: { roi: {}, last_action: "stop", voice_action: null } },
});
assert.ok(labels.includes("VOICE: none"));

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

// Bus mode uses the same sample-style card for the screen and the saved canvas.
context.performance = { timeOrigin: 1000, now: () => 10000 };
function renderBusScene({ capturedAt = 10950, mainCapturedAt = 10950, busMode = true,
  matches = [], recognized = [], status = "searching", errors = [], guidance, detections } = {}) {
  labels.length = rectangles.length = strokeColors.length = filledRects.length = 0;
  textColors.length = 0;
  drawnImages = 0;
  context.window.GOverlay.render({ image_width: 360, image_height: 640,
    captured_at_ms: mainCapturedAt, bus_mode: busMode, target_route: "7011", bus_guidance: guidance,
    walking: { mask_png: busMode === false ? null : "should-not-load", detections: [{ xyxy: [.1, .2, .3, .6], class_name: "person" }],
      event: { roi: {}, last_action: "stop" } },
    traffic: { detections: [{ xyxy: [.1, .2, .3, .3], signal_state: "red" }] },
    crosswalk: { event: { status: "search" } },
    walking_surface: { event: { status: "inside" } },
    bus: { status, captured_at_ms: capturedAt,
      event: { target_route: "7011", matches, recognized_routes: recognized, errors },
      detections: detections || [
        { class_id: 0, class_name: "bus", track_id: 12,
          box: { x1: .1, y1: .3, x2: .6, y2: .8 }, extra: { route_number: "7011" } },
        { class_id: 1, class_name: "route_number", track_id: 12,
          box: { x1: .2, y1: .35, x2: .4, y2: .4 }, extra: { text: "7011" } },
      ] },
  }, state => { drawState = state; });
}
renderBusScene();
assert.equal(drawState, "drawn");
assert.ok(labels.includes("버스 번호 확인 중"));
assert.ok(labels.includes("번호를 읽고 있어요"));
assert.ok(labels.includes("찾는 번호 7011"));
assert.equal(labels.filter(label => label === "버스 번호 확인 중").length, 2,
  "the status card and bus label share the sample's checking text");
assert.ok(strokeColors.includes("#7cbdff"));
assert.equal(textColors.find(item => item.text === "버스 번호 확인 중" && item.color === "#7cbdff")?.color, "#7cbdff");
assert.ok(filledRects.some(rect => rect.color === "#7cbdff"));
assert.ok(!strokeColors.includes("#ffd166"));
assert.ok(!labels.includes("7011"));
assert.ok(!labels.some(label => /person|ACTION|VOICE|CROSSWALK|WALKWAY|신호/.test(label)));
assert.equal(drawnImages, 0, "walking masks must not obscure the bus scene");
assert.deepEqual(rectangles[0], [36, 192, 180, 320]);

renderBusScene({ detections: [] });
assert.ok(labels.includes("버스를 찾고 있어요"));
assert.ok(textColors.some(item => item.text === "버스를 찾고 있어요" && item.color === "#b9c5d5"));
assert.ok(!strokeColors.includes("#7cbdff"));

const match = { track_id: 12, route_number: "7011", state: "matched_candidate", token_score: .98 };
renderBusScene({ matches: [match] });
assert.ok(labels.includes("7011"));
assert.ok(labels.includes("인식한 버스 번호"));
assert.ok(!labels.includes("7011번 버스 확인함"), "confirmation is shown by the prominent emerald number");
assert.ok(strokeColors.includes("#21d7bb"));
assert.ok(strokeColors.includes("#ffffff"), "number region has a distinct thin outline");

renderBusScene({ matches: [{ ...match, state: "recognized_single" }] });
assert.ok(labels.includes("7011번 버스 인식 중"));
assert.ok(strokeColors.includes("#21d7bb"), "a read target number uses the sample's mint, with candidate text until confirmation");
assert.ok(textColors.some(item => item.text === "7011" && item.color === "#21d7bb"));
assert.ok(!labels.includes("7011번 버스 확인함"));

renderBusScene({ recognized: [{ ...match, route_number: "604", is_target: false }] });
assert.ok(labels.includes("604"));
assert.ok(labels.includes("다른 버스 번호"));
assert.ok(labels.includes("다른 노선 · 목표 7011번"));
assert.ok(!strokeColors.includes("#21d7bb"));

// OCR text stays readable within its 3s evidence window; moving boxes expire at 400ms.
renderBusScene({ capturedAt: 10000, matches: [match] });
assert.ok(labels.includes("7011"));
assert.equal(rectangles.length, 1, "only the status card remains, without old camera geometry");
renderBusScene({ capturedAt: 7999, matches: [match] });
assert.ok(!labels.includes("7011"));
assert.ok(labels.includes("번호를 다시 확인하고 있어요"));
assert.equal(rectangles.length, 1, "only the searching card remains after recognition expires");
renderBusScene({ capturedAt: 12000, matches: [match] });
assert.ok(!labels.includes("7011"), "future observations cannot appear as recognized");
renderBusScene({ capturedAt: null, matches: [match] });
assert.ok(!labels.includes("7011"), "missing capture timestamps cannot validate recognition");
renderBusScene({ matches: [match], status: "error" });
assert.ok(labels.includes("번호 인식을 사용할 수 없어요"));
assert.ok(!labels.includes("7011"));
renderBusScene({ status: "loading", capturedAt: null });
assert.ok(labels.includes("번호 인식을 준비하고 있어요"));
// A delayed main response must not hide independently fresh bus number evidence.
renderBusScene({ mainCapturedAt: 10000, capturedAt: 10000, matches: [match] });
assert.equal(drawState, "drawn");
assert.ok(labels.includes("7011"), "fresh bus text survives the walking geometry deadline");
assert.equal(rectangles.length, 1, "old bus geometry still expires after 400ms");
renderBusScene({ mainCapturedAt: 10000, capturedAt: 10950, matches: [match] });
assert.ok(labels.includes("7011"));
assert.equal(rectangles.length, 3, "bus geometry uses its own fresh capture timestamp");
renderBusScene({ mainCapturedAt: 10000, capturedAt: 7999, matches: [match] });
assert.equal(drawState, "drawn");
assert.ok(!labels.includes("7011"), "delayed main responses do not extend bus evidence expiry");
assert.ok(labels.includes("번호를 다시 확인하고 있어요"));
assert.equal(rectangles.length, 1);
renderBusScene({ mainCapturedAt: 10000, capturedAt: null, matches: [match] });
assert.equal(drawState, "drawn");
assert.ok(!labels.includes("7011"), "bus evidence without a timestamp remains unverified");
assert.equal(rectangles.length, 1);
renderBusScene({ mainCapturedAt: 10000, capturedAt: 10950, matches: [match], busMode: false });
assert.equal(drawState, "stale", "explicit walking mode retains its 400ms deadline");
assert.equal(labels.length, 0);
assert.equal(rectangles.length, 0);
console.log("bus overlay: pass");

renderBusScene({ guidance: { status: "confirmed", routeNumber: "7011", confirmed: true, capturedAt: 9500 } });
assert.ok(labels.includes("7011"), "brief unreadable frames retain the same recent confirmation as speech");
assert.ok(labels.includes("인식한 버스 번호"));
assert.ok(!labels.includes("7011번 버스 확인함"), "confirmation is shown by the prominent emerald number");
assert.ok(labels.includes("버스 번호 확인 중"), "current bus geometry must not borrow a previous recognition");
renderBusScene({ guidance: { status: "confirmed", routeNumber: "7011", confirmed: true, capturedAt: 7999 } });
assert.ok(!labels.includes("7011"), "spoken-card evidence expires after 3 seconds");
renderBusScene({ guidance: { status: "other", routeNumber: "03", confirmed: true, isTarget: false, capturedAt: 9500 } });
assert.ok(labels.includes("03"));
assert.ok(labels.includes("다른 버스 번호"));
renderBusScene({ status: "error", guidance: { status: "confirmed", routeNumber: "7011", confirmed: true, capturedAt: 9500 } });
assert.ok(!labels.includes("7011"));
