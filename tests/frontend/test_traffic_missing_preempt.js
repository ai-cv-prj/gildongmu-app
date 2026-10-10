/**
 * file_path: tests/frontend/test_traffic_missing_preempt.js
 *
 * 신호 확인 불가 재생 중 신호를 다시 잡으면 색상 안내가 끊고 들어가는지,
 * 다른 음성에 밀린 신호 안내를 같은 색상이 유지되는 동안 다시 요청하는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const settings = require("./settings");

// 실제 음성 관리자와 신호 안내기를 연결한 재생 환경 구성
/** 재생 문구를 기록하고 프레임 시각과 문장 종료를 직접 제어한다. */
function harness() {
  let now = 0, frame = 0, onEnd = null;
  const spoken = [];
  const player = {
    speak(text, _validUntil, options) { spoken.push(text); onEnd = options?.onEnd || null; return true; },
    cancel() { onEnd = null; },
  };
  const context = { window: { GConfig: { get: () => settings } },
    performance: { now: () => now }, navigator: {} };
  vm.runInNewContext(fs.readFileSync("frontend/js/audio_coordinator.js", "utf8"), context);
  vm.runInNewContext(fs.readFileSync("frontend/js/guidance.js", "utf8"), context);
  const coordinator = context.window.GAudioCoordinator.create({ player, now: () => now });
  coordinator.start();
  const guidance = context.window.GGuidance.create({ coordinator, now: () => now });
  guidance.start("session", false, "traffic");
  return {
    spoken, coordinator,
    // 현재 시각의 신호 결과 전달
    /** 같은 대상 신호를 횡단보도 허용 조건과 함께 새 프레임으로 전달한다. */
    send(color, time) {
      now = time;
      guidance.accept({ session_id: "session", frame_id: ++frame,
        detections: color ? [{ track_id: 1 }] : [],
        event: { type: "traffic_signal", signal_state: color || "unknown",
          selected_detection_index: color ? 0 : null, voice_gate: { allowed: true } } }, now);
    },
    // 새 프레임 없이 시간 경과
    /** 소실 안내 생성 시점을 확인한다. */
    tick(time) { now = time; guidance.tick(); },
    // 현재 문장 재생 종료
    /** 재생기의 종료 콜백을 호출해 다음 안내를 받을 수 있게 한다. */
    end() { const callback = onEnd; onEnd = null; callback?.(); },
  };
}

test("신호 확인 불가 재생 중 초록불을 다시 잡으면 바로 끊고 안내한다", () => {
  const h = harness();
  for (const t of [0, 250, 500]) h.send("green", t);
  assert.equal(h.spoken.at(-1), "초록불, 다음 신호까지 대기");
  h.end();
  for (const t of [750, 1000, 1500, 2000, 2500]) h.send(null, t);
  h.tick(2500);
  assert.equal(h.spoken.at(-1), "신호 확인 불가");
  // 소실 안내가 끝나기 전에 같은 대상의 초록불을 다시 확정한다.
  for (const t of [2750, 3000, 3250]) h.send("green", t);
  assert.equal(h.spoken.at(-1), "초록불");
});

test("다른 음성에 밀린 신호 안내는 같은 색상이 유지되면 다시 요청한다", () => {
  const h = harness();
  // 일반 신호보다 높은 우선순위의 보행 안내가 재생 중이다.
  h.coordinator.request({ source: "walking", priority: h.coordinator.PRIORITY.walking,
    text: "오른쪽 이동.", validUntil: 10000 });
  for (const t of [0, 250, 500]) h.send("green", t);
  assert.deepEqual(h.spoken, ["오른쪽 이동."]);
  h.end();
  h.send("green", 750);
  assert.equal(h.spoken.at(-1), "초록불, 다음 신호까지 대기");
  // 수락된 뒤에는 같은 안내를 프레임마다 다시 요청하지 않는다.
  h.end();
  h.send("green", 1000);
  assert.equal(h.spoken.filter(text => text.startsWith("초록불")).length, 1);
});
