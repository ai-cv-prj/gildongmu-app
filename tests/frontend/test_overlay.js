/**
 * file_path: tests/frontend/test_overlay.js
 *
 * 모바일 장애물 오버레이에 추적 ID와 위험 이벤트 ID가 함께 표시되는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const labels = [];
const context2d = {
  beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, fill() {}, stroke() {},
  strokeRect() {}, fillRect() {}, clearRect() {}, drawImage() {}, arc() {},
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

context.window.GOverlay.render({
  image_width: 360,
  image_height: 640,
  walking: {
    detections: [{
      xyxy: [.1, .2, .3, .6], display_label: "person", alert_level: "danger",
      track_id: 12, event_id: 34,
    }],
    event: { roi: {} },
  },
  traffic: { detections: [] },
  crosswalk: { event: {} },
});

assert.ok(labels.includes("person · T12 · E34"));
console.log("overlay ids: pass");
