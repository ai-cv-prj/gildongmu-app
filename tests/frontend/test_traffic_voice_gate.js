/**
 * file_path: tests/frontend/test_traffic_voice_gate.js
 *
 * 파란 ROI의 횡단보도 허용 조건에 따른 실시간 신호 음성 차단과 재개를 검증한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const settings = require("./settings");

// 신호 안내기와 제어 가능한 프레임 시계 구성
/** 실제 안내 코드를 실행하며 음성 요청과 취소 출처를 기록한다. */
function harness() {
  const context = { window: { GConfig: { get: () => settings } } };
  vm.runInNewContext(fs.readFileSync("frontend/js/guidance.js", "utf8"), context);
  const requests = [], cleared = [];
  let now = 0, frame = 0;
  const coordinator = {
    PRIORITY: { trafficRed: 2, trafficChange: 5, traffic: 6 },
    // 실제 요청 문구 기록
    /** 재생 요청을 받아 문구와 우선순위를 보관한다. */
    request(value) { requests.push(value); return true; },
    // 취소 요청 출처 기록
    /** 다른 안내를 취소하지 않는지 확인할 출처를 보관한다. */
    clear(source) { cleared.push(source); },
  };
  const guidance = context.window.GGuidance.create({ coordinator, now: () => now });
  guidance.start("session", false, "traffic");
  return {
    requests, cleared,
    // 현재 시각의 신호 결과 전달
    /** 같은 대상 신호와 서버의 ROI 허용 결과를 새 프레임으로 전달한다. */
    send(color, time, allowed, { crossing = false, target = 1 } = {}) {
      now = time;
      const event = { type: "traffic_signal", signal_state: color || "unknown",
        selected_detection_index: color ? 0 : null };
      if (allowed !== undefined) event.voice_gate = { allowed };
      guidance.accept({ session_id: "session", frame_id: ++frame,
        detections: color ? [{ track_id: target }] : [], event, crossing_active: crossing }, now);
    },
    // 새 프레임 없이 시간 경과
    /** 소실 안내와 오래된 횡단보도 근거의 만료를 확인한다. */
    tick(time) { now = time; guidance.tick(); },
  };
}

test("ROI 밖 신호는 침묵하고 진입·재진입 후 3프레임 400ms를 확인한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, false);
  assert.equal(h.requests.length, 0);
  h.send("red", 600, true);
  h.send("red", 800, true);
  assert.equal(h.requests.length, 0);
  h.send("red", 1000, true);
  assert.equal(h.requests.at(-1).text, "빨간불");
  h.send("green", 1200, false);
  h.tick(4000);
  assert.equal(h.requests.length, 1);
  assert.ok(h.cleared.every(source => source === "traffic"));
  for (const t of [4000, 4200, 4400]) h.send("green", t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불, 다음 신호까지 대기"]);
});

test("ROI에 다시 들어오면 이전과 같은 빨간불도 새로 안내한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true);
  h.send("red", 600, false);
  for (const t of [800, 1000, 1200]) h.send("red", t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "빨간불"]);
});

test("신호 확인 불가는 최신 ROI 조건을 충족할 때만 생성한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true);
  for (const t of [1000, 1800, 2400]) h.send(null, t, true);
  h.tick(2400);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "신호 확인 불가"]);
  h.send(null, 2600, false);
  h.tick(6000);
  assert.equal(h.requests.length, 2);
});

test("허용 정보 누락과 프레임 소실은 신호 안내를 차단한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t);
  assert.equal(h.requests.length, 0);
  for (const t of [600, 800, 1000]) h.send("red", t, true);
  const clears = h.cleared.length;
  h.tick(2500);
  assert.equal(h.cleared.length, clears + 1);
  h.tick(5000);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불"]);
});

test("횡단 중 짧게 끊긴 뒤 다른 대상의 초록불은 대기 문구 없이 안내한다", () => {
  const h = harness();
  const crossing = { crossing: true };
  for (const t of [0, 200, 400]) h.send("green", t, true, crossing);
  h.send("green", 600, false, crossing);
  h.send("green", 800, false, crossing);
  assert.ok(h.cleared.includes("traffic"));
  for (const t of [1000, 1200, 1400]) h.send("green", t, true, { crossing: true, target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["초록불, 다음 신호까지 대기", "초록불"]);
});

test("횡단이 끝났거나 근거가 오래 끊기면 다시 잡은 초록불을 처음처럼 안내한다", () => {
  for (const [crossing, lostMs] of [[false, 200], [true, 5000]]) {
    const h = harness();
    for (const t of [0, 200, 400]) h.send("green", t, true, { crossing: true });
    h.send("green", 600, false, { crossing });
    h.send("green", 600 + lostMs, false, { crossing });
    const start = 800 + lostMs;
    for (const t of [start, start + 200, start + 400]) h.send("green", t, true, { crossing, target: 2 });
    assert.deepEqual(h.requests.map(item => item.text),
      ["초록불, 다음 신호까지 대기", "초록불, 다음 신호까지 대기"]);
  }
});
