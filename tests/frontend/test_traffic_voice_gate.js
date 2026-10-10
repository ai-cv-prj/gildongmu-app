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
    send(color, time, allowed, { crossing = false, target = 1, confidence } = {}) {
      now = time;
      const event = { type: "traffic_signal", signal_state: color || "unknown",
        selected_detection_index: color ? 0 : null };
      if (allowed !== undefined) event.voice_gate = { allowed };
      guidance.accept({ session_id: "session", frame_id: ++frame,
        detections: color ? [{ track_id: target, confidence }] : [], event, crossing_active: crossing }, now);
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

test("초록불로 바뀐 뒤 건너는 중 짧게 끊기면 다른 대상의 초록불은 대기 문구 없이 안내한다", () => {
  const h = harness();
  const crossing = { crossing: true };
  for (const t of [0, 200, 400]) h.send("red", t, true, crossing);
  for (const t of [600, 800, 1000]) h.send("green", t, true, crossing);
  h.send("green", 1200, false, crossing);
  h.send("green", 1400, false, crossing);
  assert.ok(h.cleared.includes("traffic"));
  for (const t of [1600, 1800, 2000]) h.send("green", t, true, { crossing: true, target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜", "초록불"]);
});

test("횡단이 끝났거나 근거가 오래 끊기면 다시 잡은 초록불을 처음처럼 안내한다", () => {
  for (const [crossing, lostMs] of [[false, 200], [true, 5000]]) {
    const h = harness();
    for (const t of [0, 200, 400]) h.send("red", t, true, { crossing: true });
    for (const t of [600, 800, 1000]) h.send("green", t, true, { crossing: true });
    h.send("green", 1200, false, { crossing });
    h.send("green", 1200 + lostMs, false, { crossing });
    const start = 1400 + lostMs;
    for (const t of [start, start + 200, start + 400]) h.send("green", t, true, { crossing, target: 2 });
    assert.deepEqual(h.requests.map(item => item.text),
      ["빨간불", "초록불로 바뀜", "초록불, 다음 신호까지 대기"]);
  }
});

test("대기 안내 뒤에는 건너는 중 끊겨도 다시 잡은 초록불을 대기 안내한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("green", t, true, { crossing: true });
  h.send("green", 600, false, { crossing: true });
  for (const t of [800, 1000, 1200]) h.send("green", t, true, { crossing: true, target: 2 });
  assert.deepEqual(h.requests.map(item => item.text),
    ["초록불, 다음 신호까지 대기", "초록불, 다음 신호까지 대기"]);
});

test("빨간불 대기 중에는 횡단 상태여도 끊긴 뒤 신호를 새로 확인한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true, { crossing: true });
  h.send("red", 600, false, { crossing: true });
  for (const t of [800, 1000, 1200]) h.send("green", t, true, { crossing: true, target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불, 다음 신호까지 대기"]);
});

test("순간 오인식 한 프레임이 섞여도 최근 다수 색상으로 전환을 확정한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true);
  for (const [color, t] of [["green", 600], ["green", 800], ["red", 1000], ["green", 1200]]) h.send(color, t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜"]);
});

test("신호를 잠깐 놓쳐도 5초 안에는 직전 색상을 기억해 전환으로 안내한다", () => {
  for (const [nullTimes, start, expected] of [
    [[1000, 1800, 2400], 2600, "초록불로 바뀜"],
    [[1000, 1800, 2400, 3200, 4000, 4800, 5600], 6000, "초록불"],
  ]) {
    const h = harness();
    for (const t of [0, 200, 400]) h.send("red", t, true);
    for (const t of nullTimes) { h.send(null, t, true); h.tick(t); }
    for (const t of [start, start + 200, start + 400]) h.send("green", t, true);
    assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "신호 확인 불가", expected]);
  }
});

test("빨간불을 보던 중 다른 신호등의 초록은 대기 안내한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true);
  for (const t of [600, 800, 1000]) h.send("green", t, true, { target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불, 다음 신호까지 대기"]);
});

test("초록 도중 흐린 붉은 오검출과 오래된 빨강 관측으로 빨간불 전환을 안내하지 않는다", () => {
  const h = harness();
  for (const t of [0, 250, 500, 750, 1000]) h.send("green", t, true, { confidence: .75 });
  for (const [color, t, confidence] of [["red", 1250, .47], ["red", 1500, .34], ["green", 2000, .78],
    ["red", 2250, .2], ["red", 2500, .17], ["red", 2750, .21], ["green", 3250, .77], ["red", 4250, .52]]) {
    h.send(color, t, true, { confidence });
  }
  assert.deepEqual(h.requests.map(item => item.text), ["초록불, 다음 신호까지 대기"]);
});

test("초록에서 빨강으로의 전환은 800ms 이상 빨강을 확인한 뒤 안내한다", () => {
  const h = harness();
  for (const t of [0, 200, 400]) h.send("red", t, true);
  for (const t of [600, 800, 1000]) h.send("green", t, true);
  for (const t of [1200, 1400, 1600]) h.send("red", t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜"]);
  for (const t of [1800, 2000]) h.send("red", t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜", "빨간불로 바뀜"]);
});
