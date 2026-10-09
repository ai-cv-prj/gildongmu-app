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
  PRIORITY: { emergency: 0, trafficRed: 2, walkingSurface: 3, walking: 4,
    trafficChange: 5, traffic: 6 },
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
    event: { type: "traffic_signal", selected_detection_index: 0, signal_state: "red",
      voice_gate: { allowed: true } } }, time);
}
assert.equal(spoken.filter(text => text === "빨간불").length, 1);

// 새 위험 이벤트는 신호 안내와 독립적으로 수신
const walking = context.window.GGuidance.create({ coordinator, now: () => now });
walking.start("test", false, "walking");
assert.equal(spoken.some(text => text.includes("안내를 시작합니다")), false);
now = 2400;
walking.accept({ session_id: "test", frame_id: 1,
  captured_at_ms: 2400,
  event: { type: "walking_warning", level: "danger",
    last_action: "right", voice_text: "오른쪽 한 걸음" } }, now);
assert.ok(spoken.includes("오른쪽 한 걸음"));
assert.equal(requests.at(-1).metadata.action, "right");
assert.equal(requests.at(-1).metadata.frame_id, 1);
assert.equal(requests.at(-1).metadata.captured_at_ms, 2400);

// 혼잡과 방향 판단 불가 행동도 서버가 확정한 문구 그대로 재생
now = 2450;
walking.accept({ session_id: "test", frame_id: 2, captured_at_ms: now,
  event: { voice_event: { action: "crowded", text: "혼잡 주의", event_id: 1 } } }, now);
assert.equal(spoken.at(-1), "혼잡 주의");
now = 2475;
walking.accept({ session_id: "test", frame_id: 3, captured_at_ms: now,
  event: { voice_event: { action: "blocked", text: "전방 장애물", event_id: 2 } } }, now);
assert.equal(spoken.at(-1), "전방 장애물");

// 횡단 중 차량 행동이 없으면 진입 전에 재생하던 일반 장애물 음성을 중단
now = 2500;
walking.accept({ session_id: "test", frame_id: 4, crossing_active: true,
  event: { type: "walking_warning", level: "danger", last_action: "stop",
    voice_action: null } }, now);
assert.equal(cleared.at(-1), "walking");

// 횡단 접근 중 차량 행동이 없으면 기존 일반 장애물 음성을 중단
now = 2600;
const clearedBeforeApproach = cleared.length;
walking.accept({ session_id: "test", frame_id: 5, crossing_active: false,
  crosswalk_status: "approach",
  event: { type: "walking_warning", level: "danger", last_action: "stop",
    voice_action: null } }, now);
assert.equal(cleared.length, clearedBeforeApproach + 1);
assert.equal(cleared.at(-1), "walking");
console.log("guidance: pass");

// A server-authorized surface stop is independent of the summary's caution level.
now = 2700;
walking.accept({ session_id: "test", frame_id: 6, captured_at_ms: 2700,
  event: { type: "walking_warning", level: "caution", voice_action: "stop",
    voice_event: { action: "stop", text: "멈추세요.", source: "surface", urgency: "emergency" } } }, now);
assert.equal(spoken.at(-1), "멈추세요.");
assert.equal(requests.at(-1).priority, 0);

// A new stop event must interrupt bus input even when the action stays stop.
now = 2800;
walking.accept({ session_id: "test", frame_id: 7, captured_at_ms: 2800,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 1 } } }, now);
const stops = spoken.filter(text => text === "멈추세요.").length;
now = 2900;
walking.accept({ session_id: "test", frame_id: 8, captured_at_ms: 2900,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 1 } } }, now);
assert.equal(spoken.filter(text => text === "멈추세요.").length, stops);
now = 3000;
walking.accept({ session_id: "test", frame_id: 9, captured_at_ms: 3000,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 2 } } }, now);
assert.equal(spoken.filter(text => text === "멈추세요.").length, stops + 1);

// 안내 없음 응답은 새 음성을 만들지 않고 명시적인 해제 요청만 전달한다.
const beforeNone = spoken.length;
now = 3100;
walking.accept({ session_id: "test", frame_id: 10, captured_at_ms: now,
  event: { type: "walking_warning", level: "danger", last_action: null,
    voice_action: null, voice_clear: true } }, now);
assert.equal(spoken.length, beforeNone);
assert.equal(cleared.at(-1), "walking");
now = 3200;
walking.accept({ session_id: "test", frame_id: 11, captured_at_ms: now,
  event: { type: "walking_warning", level: "danger", last_action: "unsupported",
    voice_text: "지원하지 않는 행동", voice_event: { action: "unsupported", text: "지원하지 않는 행동" } } }, now);
assert.equal(spoken.length, beforeNone);
now = 3300;
walking.accept({ session_id: "test", frame_id: 12, captured_at_ms: now,
  event: { voice_event: { action: "stop", text: "멈추세요.", event_id: 4 } } }, now);
assert.equal(spoken.at(-1), "멈추세요.");
now = 3400;
walking.accept({ session_id: "test", frame_id: 13, captured_at_ms: now,
  event: { voice_event: { action: "left", text: "왼쪽 두 걸음", event_id: 5 } } }, now);
assert.equal(spoken.at(-1), "왼쪽 두 걸음");
assert.equal(requests.at(-1).priority, 4);

// 혼잡은 서버가 3초마다 갱신한 이벤트만 재안내하고 동일 이벤트는 중복 재생하지 않는다.
const beforeCrowd = spoken.filter(text => text === "혼잡 주의").length;
for (const [frame, time, eventId] of [[14, 3500, 6], [15, 5000, 6], [16, 6500, 7]]) {
  now = time;
  walking.accept({ session_id: "test", frame_id: frame, captured_at_ms: now,
    event: { voice_event: { action: "crowded", text: "혼잡 주의", event_id: eventId } } }, now);
}
assert.equal(spoken.filter(text => text === "혼잡 주의").length, beforeCrowd + 2);

// 방향 철회는 재생 취소로 전달하며 이후 안전한 새 방향 이벤트를 다시 허용한다.
now = 6600;
walking.accept({ session_id: "test", frame_id: 17, captured_at_ms: now,
  event: { voice_action: null, voice_clear: true } }, now);
assert.equal(cleared.at(-1), "walking");
now = 6700;
walking.accept({ session_id: "test", frame_id: 18, captured_at_ms: now,
  event: { voice_event: { action: "left", text: "왼쪽 한 걸음", event_id: 8 } } }, now);
assert.equal(spoken.at(-1), "왼쪽 한 걸음");
