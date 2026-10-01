/**
 * file_path: tests/frontend/test_audio_coordinator.js
 *
 * 전역 음성 관리자의 우선순위·횡단보도 반복·복귀 중단 정책을 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

let now = 1000;
const spoken = [];
let onEnd = null, cancelCount = 0;
const player = {
  speak(text, _validUntil, options) {
    spoken.push(text);
    onEnd = options?.onEnd || null;
    return true;
  },
  cancel() { cancelCount++; onEnd = null; },
};
const settings = require("./settings");
const context = { window: { GConfig: { get: () => settings } }, performance: { now: () => now }, navigator: {} };
vm.runInNewContext(fs.readFileSync("frontend/js/audio_coordinator.js", "utf8"), context);
const coordinator = context.window.GAudioCoordinator.create({ player, now: () => now });
coordinator.start();

// 하위 신호 안내를 횡단보도 이탈이 즉시 중단하고 이탈 음성만 반복한다.
coordinator.request({ source: "traffic", priority: 4, text: "빨간불입니다.", validUntil: 3000 });
coordinator.acceptCrosswalk({ status: "outside_left", repeat: true,
  voice_text: "횡단보도 이탈! 오른쪽으로 이동하세요!" }, now);
assert.equal(cancelCount, 2);
assert.equal(spoken.at(-1), "횡단보도 이탈! 오른쪽으로 이동하세요!");
assert.equal(coordinator.request({ source: "walking", priority: 2,
  text: "가운데에 차량.", validUntil: 3000 }), false);
onEnd();
assert.equal(spoken.filter(text => text.startsWith("횡단보도 이탈!")).length, 2);

// 이탈 방향이 바뀌면 같은 1순위라도 이전 문장을 취소하고 최신 방향으로 교체한다.
coordinator.acceptCrosswalk({ status: "outside_right", repeat: true,
  voice_text: "횡단보도 이탈! 왼쪽으로 이동하세요!" }, now + 50);
assert.equal(spoken.at(-1), "횡단보도 이탈! 왼쪽으로 이동하세요!");
assert.equal(cancelCount, 3);

// 순간적인 카메라·경계 불확실 상태는 진행 중인 이탈 음성을 끊지 않는다.
coordinator.acceptCrosswalk({ status: "uncertain", crossing_active: true,
  stale_after_ms: 1500 }, now + 80);
assert.equal(cancelCount, 3);
onEnd();
assert.equal(spoken.at(-1), "횡단보도 이탈! 왼쪽으로 이동하세요!");

// 복귀 판정을 받으면 반복 음성을 즉시 중단한다.
coordinator.acceptCrosswalk({ status: "crossing", repeat: false }, now + 100);
assert.equal(cancelCount, 4);

// 진입 전 정렬 안내를 반복하고 발이 경계 안에 들어오면 즉시 중단한다.
coordinator.acceptCrosswalk({ status: "align_right", repeat: true,
  voice_text: "오른쪽으로 이동하세요!" }, now + 150);
assert.equal(spoken.at(-1), "오른쪽으로 이동하세요!");
coordinator.acceptCrosswalk({ status: "crossing", repeat: false }, now + 160);
assert.equal(cancelCount, 5);

// 방향 미확정 상태는 이탈 음성을 시작하지 않는다.
coordinator.acceptCrosswalk({ status: "outside_unknown", repeat: true,
  voice_text: "횡단보도 이탈!" }, now + 180);
assert.equal(spoken.at(-1), "오른쪽으로 이동하세요!");

// 가장자리 상태에서는 음성을 시작하지 않는다.
const edge = { status: "edge", event_id: 8 };
coordinator.acceptCrosswalk(edge, now + 200);
coordinator.acceptCrosswalk(edge, now + 300);
assert.equal(spoken.at(-1), "오른쪽으로 이동하세요!");

// 오래된 이탈 응답은 틱에서 반복을 중단한다.
coordinator.acceptCrosswalk({ status: "outside_right", repeat: true,
  voice_text: "횡단보도 이탈! 왼쪽으로 이동하세요!" }, now + 300);
now = 3000;
coordinator.tick();
assert.equal(cancelCount, 6);
console.log("audio coordinator: pass");
