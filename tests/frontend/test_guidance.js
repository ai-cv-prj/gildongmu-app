/**
 * file_path: tests/frontend/test_guidance.js
 *
 * 세 모델의 느린 프레임 간격에서도 신호와 보행 음성이 한 화면에서 동작하는지 확인한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const settings = require("./settings");
const context = { window: { GConfig: { get: () => settings } }, performance: { now: () => 0 } };
vm.runInNewContext(fs.readFileSync("frontend/js/guidance.js", "utf8"), context);
const spoken = [];
let now = 0;
const cleared = [];
const coordinator = {
  PRIORITY: { trafficRed: 2, walking: 3, trafficChange: 4, traffic: 5 },
  request({ text }) {
    spoken.push(text);
    return true;
  },
  clear(source) { cleared.push(source); },
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
  event: { type: "walking_warning", level: "danger",
    voice_text: "오른쪽으로 이동하세요." } }, now);
assert.ok(spoken.includes("오른쪽으로 이동하세요."));

// 횡단 중 차량 행동이 없으면 진입 전에 재생하던 일반 장애물 음성을 중단
now = 2500;
walking.accept({ session_id: "test", frame_id: 2, crossing_active: true,
  event: { type: "walking_warning", level: "danger", last_action: "stop",
    voice_action: null } }, now);
assert.equal(cleared.at(-1), "walking");
console.log("guidance: pass");
