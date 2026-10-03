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
const requests = [];
let now = 0;
const cleared = [];
const coordinator = {
  PRIORITY: { trafficRed: 2, walking: 3, trafficChange: 4, traffic: 5 },
  request(request) {
    const { text } = request;
    spoken.push(text);
    requests.push(request);
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
  captured_at_ms: 2400,
  event: { type: "walking_warning", level: "danger",
    last_action: "right", voice_text: "오른쪽으로 이동하세요." } }, now);
assert.ok(spoken.includes("오른쪽으로 이동하세요."));
assert.equal(requests.at(-1).metadata.action, "right");
assert.equal(requests.at(-1).metadata.frame_id, 1);
assert.equal(requests.at(-1).metadata.captured_at_ms, 2400);

// 횡단 중 차량 행동이 없으면 진입 전에 재생하던 일반 장애물 음성을 중단
now = 2500;
walking.accept({ session_id: "test", frame_id: 2, crossing_active: true,
  event: { type: "walking_warning", level: "danger", last_action: "stop",
    voice_action: null } }, now);
assert.equal(cleared.at(-1), "walking");
console.log("guidance: pass");

// A server-authorized surface stop is independent of the summary's caution level.
now = 2700;
walking.accept({ session_id: "test", frame_id: 3, captured_at_ms: 2700,
  event: { type: "walking_warning", level: "caution", voice_action: "stop",
    voice_event: { action: "stop", text: "멈추세요.", source: "surface", urgency: "emergency" } } }, now);
assert.equal(spoken.at(-1), "멈추세요.");
assert.equal(requests.at(-1).priority, 0);

// A new stop event must interrupt bus input even when the action stays stop.
now = 2800;
walking.accept({ session_id: "test", frame_id: 4, captured_at_ms: 2800,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 1 } } }, now);
const stops = spoken.filter(text => text === "멈추세요.").length;
now = 2900;
walking.accept({ session_id: "test", frame_id: 5, captured_at_ms: 2900,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 1 } } }, now);
assert.equal(spoken.filter(text => text === "멈추세요.").length, stops);
now = 3000;
walking.accept({ session_id: "test", frame_id: 6, captured_at_ms: 3000,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 2 } } }, now);
assert.equal(spoken.filter(text => text === "멈추세요.").length, stops + 1);
