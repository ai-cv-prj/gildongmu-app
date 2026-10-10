/**
 * file_path: tests/frontend/test_traffic_repeat.js
 *
 * 같은 신호가 계속 확인될 때 일정 간격으로 다시 안내하는지 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const settings = require("./settings");

const PRIORITY = { trafficRed: 2, trafficChange: 5, traffic: 6 };

// 신호 안내기와 제어 가능한 프레임 시계 구성
/** 실제 안내 코드를 실행하며 음성 요청을 기록한다. */
function harness() {
  const context = { window: { GConfig: { get: () => settings } } };
  vm.runInNewContext(fs.readFileSync("frontend/js/guidance.js", "utf8"), context);
  const requests = [];
  let now = 0, frame = 0;
  const coordinator = { PRIORITY, request(value) { requests.push(value); return true; }, clear() {} };
  const guidance = context.window.GGuidance.create({ coordinator, now: () => now });
  guidance.start("session", false, "traffic");
  return {
    requests,
    // 허용된 같은 대상 신호를 일정 간격 프레임으로 전달
    /** start부터 end까지 step 간격으로 같은 색상을 보낸다. */
    send(color, start, end, step = 250) {
      for (let time = start; time <= end; time += step) {
        now = time;
        guidance.accept({ session_id: "session", frame_id: ++frame, detections: [{ track_id: 1 }],
          event: { type: "traffic_signal", signal_state: color, selected_detection_index: 0,
            voice_gate: { allowed: true } } }, now);
      }
    },
  };
}

test("같은 빨간불은 5초마다 낮은 우선순위로 다시 안내한다", () => {
  const h = harness();
  h.send("red", 0, 11000);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "빨간불", "빨간불"]);
  assert.deepEqual(h.requests.map(item => item.priority), [PRIORITY.trafficRed, PRIORITY.traffic, PRIORITY.traffic]);
  assert.equal(h.requests[1].metadata.repeat, true);
});

test("5초가 지나기 전에는 다시 안내하지 않는다", () => {
  const h = harness();
  h.send("red", 0, 5250);
  assert.equal(h.requests.length, 1);
});

test("첫 초록 대기 안내는 같은 문구로, 전환 뒤에는 현재 색상만 다시 안내한다", () => {
  const waiting = harness();
  waiting.send("green", 0, 5500);
  assert.deepEqual(waiting.requests.map(item => item.text),
    ["초록불, 다음 신호까지 대기", "초록불, 다음 신호까지 대기"]);
  const changed = harness();
  changed.send("red", 0, 500);
  changed.send("green", 750, 6250);
  assert.deepEqual(changed.requests.map(item => item.text), ["빨간불", "초록불로 바뀜", "초록불"]);
});
