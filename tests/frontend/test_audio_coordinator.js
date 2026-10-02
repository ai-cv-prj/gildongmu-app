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
  text: "직진하세요.", validUntil: 3000 }), false);
// 보행 행동이 오른쪽으로 유지되거나 없어도 이탈 음성은 끊거나 재시작하지 않는다.
const exitCount = spoken.length;
assert.equal(coordinator.request({ source: "walking", priority: coordinator.PRIORITY.walking,
  text: "오른쪽으로 이동하세요.", validUntil: 3000 }), false);
coordinator.acceptCrosswalk({ status: "outside_left", repeat: true,
  voice_text: "횡단보도 이탈! 오른쪽으로 이동하세요!" }, now + 10);
assert.equal(spoken.length, exitCount);
assert.equal(cancelCount, 2);
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

// 복귀 판정을 받으면 현재 문장은 유지하고 다음 반복만 보류한다.
coordinator.acceptCrosswalk({ status: "crossing", repeat: false }, now + 100);
assert.equal(cancelCount, 3);

// 현재 문장이 끝나기 전 같은 방향으로 재이탈하면 끊거나 재시작하지 않고 반복을 잇는다.
coordinator.acceptCrosswalk({ status: "outside_right", repeat: true,
  voice_text: "횡단보도 이탈! 왼쪽으로 이동하세요!" }, now + 110);
assert.equal(cancelCount, 3);
const sameDirectionCount = spoken.filter(text => text === "횡단보도 이탈! 왼쪽으로 이동하세요!").length;
onEnd();
assert.equal(spoken.filter(text => text === "횡단보도 이탈! 왼쪽으로 이동하세요!").length,
  sameDirectionCount + 1);

// 다시 복귀하면 재생 중인 문장을 끝낸 뒤 추가 반복하지 않는다.
coordinator.acceptCrosswalk({ status: "crossing", repeat: false }, now + 120);
const returnedCount = spoken.length;
onEnd();
assert.equal(spoken.length, returnedCount);
assert.equal(cancelCount, 3);

// 장애물 안내 중 빨간불이 확정되면 즉시 중단하고 빨간불을 안내한다.
coordinator.request({ source: "walking", priority: coordinator.PRIORITY.walking,
  text: "직진하세요.", validUntil: 4000 });
coordinator.request({ source: "traffic", priority: coordinator.PRIORITY.trafficRed,
  text: "빨간불입니다.", validUntil: 4000 });
assert.equal(spoken.at(-1), "빨간불입니다.");
assert.equal(cancelCount, 4);

// 진입 전 정렬 안내를 반복하고 발이 경계 안에 들어오면 즉시 중단한다.
coordinator.acceptCrosswalk({ status: "align_right", repeat: true,
  voice_text: "오른쪽으로 이동하세요!" }, now + 150);
assert.equal(spoken.at(-1), "오른쪽으로 이동하세요!");
coordinator.acceptCrosswalk({ status: "crossing", repeat: false }, now + 160);
assert.equal(cancelCount, 6);

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
assert.equal(cancelCount, 7);

console.log("audio coordinator: pass");

// Once playback starts, the frame start deadline must not truncate a one-shot clip.
let runningClip;
let completed = 0, interrupted = 0;
const startedPlayer = { speak(_text, _deadline, options) { runningClip = options; options.onStart(); return true; },
  cancel() {} };
const playback = context.window.GAudioCoordinator.create({ player: startedPlayer, now: () => now });
playback.start();
playback.request({ source: "boarding-stop", priority: 0, text: "멈추세요.", validUntil: now + 100,
  onComplete: () => completed++, onCancel: () => interrupted++ });
now += 200;
playback.tick();
assert.equal(interrupted, 0);
assert.equal(completed, 0);
runningClip.onEnd();
assert.equal(completed, 1);
playback.request({ source: "boarding", priority: 6, text: "버스 번호", validUntil: now + 1000,
  onCancel: () => interrupted++ });
playback.request({ source: "walking", priority: 0, text: "멈추세요.", validUntil: now + 1000 });
assert.equal(interrupted, 1);
