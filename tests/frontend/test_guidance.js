/**
 * file_path: tests/frontend/test_guidance.js
 *
 * 세 모델의 느린 프레임 간격에서도 신호와 보행 음성이 한 화면에서 동작하는지 확인한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const context = { window: {}, performance: { now: () => 0 } };
vm.runInNewContext(fs.readFileSync("frontend/js/guidance.js", "utf8"), context);
const spoken = [];
let now = 0;
const coordinator = {
  PRIORITY: { walking: 2, trafficChange: 3, traffic: 4 },
  request({ text }) {
    spoken.push(text);
    return true;
  },
  clear() {},
};

// 1초가 넘는 간격에서도 세 프레임 확인 후 한 번 안내
const traffic = context.window.GGuidance.create({ coordinator, now: () => now });
traffic.start("test", false, "traffic");
assert.deepEqual(spoken, []);
for (const [index, time] of [100, 1200, 2300].entries()) {
  now = time;
  traffic.accept({ session_id: "test", frame_id: index + 1,
    detections: [{ track_id: 5 }],
    event: { type: "traffic_signal", selected_detection_index: 0, signal_state: "red" } }, time);
}
assert.equal(spoken.filter(text => text === "빨간불입니다.").length, 1);

// 새 위험 이벤트는 신호 안내와 독립적으로 수신
const walking = context.window.GGuidance.create({ coordinator, now: () => now });
walking.start("test", false, "walking");
assert.equal(spoken.some(text => text.includes("안내를 시작합니다")), false);
now = 2400;
walking.accept({ session_id: "test", frame_id: 1,
  event: { type: "walking_warning", level: "danger", voice_event_ids: [9],
    voice_text: "왼쪽에 사람." } }, now);
assert.ok(spoken.includes("왼쪽에 사람."));
console.log("guidance: pass");
