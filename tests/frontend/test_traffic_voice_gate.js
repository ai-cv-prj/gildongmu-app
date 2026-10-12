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
// 신호 확인 불가 기준 시간은 설정값을 따른다.
const MISSING = settings.audio.guidance.missing_ms;

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
    send(color, time, allowed, { crossing = false, target = 1, confidence, box } = {}) {
      now = time;
      const event = { type: "traffic_signal", signal_state: color || "unknown",
        selected_detection_index: color ? 0 : null };
      if (allowed !== undefined) event.voice_gate = { allowed };
      guidance.accept({ session_id: "session", frame_id: ++frame,
        detections: color ? [{ track_id: target, confidence, xyxy: box }] : [], event, crossing_active: crossing }, now);
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
  const lost = 400 + MISSING;
  for (let t = 1000; t < lost; t += 800) h.send(null, t, true);
  h.send(null, lost, true);
  h.tick(lost);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "신호 확인 불가"]);
  h.send(null, lost + 200, false);
  h.tick(lost + 3600);
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
  // 대기 문구 없이 같은 초록으로 이어 가며, 반복 간격 안이라 다시 읽지 않는다.
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜"]);
});

test("횡단이 끝났거나 근거가 오래 끊기면 다시 잡은 초록불을 처음처럼 안내한다", () => {
  for (const [crossing, lostMs] of [[false, 2000], [true, 5000]]) {
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
  const lost = 400 + MISSING;
  for (const [until, start, expected] of [
    [lost, lost + 200, "초록불로 바뀜"],
    [6000, 6000, "초록불"],
  ]) {
    const h = harness();
    for (const t of [0, 200, 400]) h.send("red", t, true);
    const nullTimes = [];
    for (let t = 1000; t < until; t += 800) nullTimes.push(t);
    nullTimes.push(lost);
    for (const t of nullTimes.sort((a, b) => a - b)) { h.send(null, t, true); h.tick(t); }
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

test("최초 초록의 대기 상태는 같은 ID·다른 ID 재인식으로 해제되지 않는다", () => {
  for (const target of [1, 2]) {
    const h = harness();
    for (const t of [0, 250, 500]) h.send("green", t, true);
    const back = 500 + MISSING + 250;
    for (let t = 750; t < back; t += 250) { h.send(null, t, true); h.tick(t); }
    for (const t of [back, back + 250, back + 500]) h.send("green", t, true, { target });
    assert.deepEqual(h.requests.map(item => item.text).filter(text => text.startsWith("초록불")),
      ["초록불, 다음 신호까지 대기", "초록불, 다음 신호까지 대기"], `target ${target}`);
    // 빨강→초록 전환을 확인한 뒤에야 일반 초록 안내로 바뀐다.
    for (let t = back + 750; t <= back + 1750; t += 250) h.send("red", t, true, { target });
    for (const t of [back + 2000, back + 2250, back + 2500]) h.send("green", t, true, { target });
    assert.equal(h.requests.at(-1).text, "초록불로 바뀜");
  }
});

test("횡단 중 프레임 수신이 끊겨도 5초 안에 돌아오면 초기 대기로 돌아가지 않는다", () => {
  for (const [stallMs, expectWait] of [[1500, false], [3000, false], [6000, true]]) {
    const h = harness();
    const crossing = { crossing: true };
    for (const t of [0, 250, 500]) h.send("red", t, true, crossing);
    for (const t of [750, 1000, 1250]) h.send("green", t, true, crossing);
    for (let t = 1500; t <= 1250 + stallMs; t += 250) h.tick(t);
    const back = 1250 + stallMs + 250;
    for (const t of [back, back + 250, back + 500]) h.send("green", t, true, crossing);
    const texts = h.requests.map(item => item.text);
    assert.equal(texts.slice(2).includes("초록불, 다음 신호까지 대기"), expectWait, `stall ${stallMs}ms: ${texts}`);
  }
});

test("4·5·10·15·30fps 모두에서 첫 신호와 빨강↔초록 전환을 안내한다", () => {
  for (const fps of [4, 5, 10, 15, 30]) {
    const h = harness();
    const step = 1000 / fps;
    let t = 0;
    const run = (color, ms) => { const end = t + ms; for (; t < end; t += step) h.send(color, Math.round(t), true); };
    run("red", 1500); run("green", 2000); run("red", 2000);
    assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜", "빨간불로 바뀜"], `${fps}fps`);
  }
});

test("수신 중단 뒤 복귀 첫 프레임의 일시적 미인식으로 횡단 이력을 지우지 않는다", () => {
  const h = harness();
  const crossing = { crossing: true };
  for (const t of [0, 250, 500]) h.send("red", t, true, crossing);
  for (const t of [750, 1000, 1250]) h.send("green", t, true, crossing);
  for (let t = 1500; t <= 4250; t += 250) h.tick(t);
  // 서버는 2초 넘는 공백 뒤 횡단 상태를 초기화하고, 첫 프레임은 횡단보도도 놓친다.
  h.send(null, 4500, false, { crossing: false });
  for (const t of [4750, 5000, 5250]) h.send("green", t, true, crossing);
  assert.equal(h.requests.map(item => item.text).includes("초록불, 다음 신호까지 대기"), false,
    h.requests.map(item => item.text).join(" / "));
});

test("수신 중단 뒤 미인식이 마지막 프레임부터 5초를 넘기면 기존처럼 초기화한다", () => {
  const h = harness();
  const crossing = { crossing: true };
  for (const t of [0, 250, 500]) h.send("red", t, true, crossing);
  for (const t of [750, 1000, 1250]) h.send("green", t, true, crossing);
  for (let t = 1500; t <= 4250; t += 250) h.tick(t);
  for (const t of [4500, 5000, 5500, 6000, 6500]) h.send(null, t, false, { crossing: false });
  for (const t of [6750, 7000, 7250]) h.send("green", t, true, crossing);
  assert.equal(h.requests.at(-1).text, "초록불, 다음 신호까지 대기");
});

test("빨간불 뒤 가려져 ID가 바뀌어도 같은 자리의 초록이면 전환으로 안내한다", () => {
  const h = harness();
  const near = [0.68, 0.31, 0.71, 0.37], moved = [0.80, 0.30, 0.84, 0.35];
  for (const t of [0, 250, 500, 750]) h.send("red", t, true, { box: near });
  for (const t of [1000, 1500, 2000]) h.send(null, t, true);
  for (const t of [2250, 2500, 2750]) h.send("green", t, true, { target: 2, box: moved });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜"]);
});

test("빨간불 대기 중 횡단보도 판정이 잠깐 끊겨도 같은 신호의 초록은 전환으로 안내한다", () => {
  const h = harness();
  for (const t of [0, 250, 500, 750]) h.send("red", t, true);
  h.send(null, 1000, false);
  for (const t of [1250, 1500, 1750]) h.send("green", t, true);
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "초록불로 바뀜"]);
});

test("빨간불 뒤 멀리 떨어진 신호등의 초록이나 오래 지난 초록은 대기 안내한다", () => {
  const cases = [
    { box: [0.039, 0.288, 0.089, 0.351], start: 2250 },
    { box: [0.68, 0.31, 0.71, 0.37], start: 5000 },
  ];
  for (const { box, start } of cases) {
    const h = harness();
    for (const t of [0, 250, 500, 750]) h.send("red", t, true, { box: [0.535, 0.237, 0.574, 0.296] });
    for (let t = 1000; t < start; t += 500) { h.send(null, t, true); h.tick(t); }
    for (const t of [start, start + 250, start + 500]) h.send("green", t, true, { target: 2, box });
    assert.equal(h.requests.filter(item => !item.text.startsWith("신호")).at(-1).text, "초록불, 다음 신호까지 대기",
      JSON.stringify({ box, start }));
  }
});

test("초록불로 바뀐 직후 횡단 판정 전의 1프레임 끊김으로 대기로 돌아가지 않는다", () => {
  const h = harness();
  for (const t of [0, 250, 500, 750]) h.send("red", t, true);
  for (const t of [1000, 1250, 1500]) h.send("green", t, true);
  h.send("green", 1750, false);
  for (const t of [2000, 2250, 2500]) h.send("green", t, true);
  for (let t = 2750; t <= 7000; t += 250) h.send("green", t, true);
  assert.equal(h.requests.some(item => item.text === "초록불, 다음 신호까지 대기"), false,
    h.requests.map(item => item.text).join(" / "));
});

test("초록불로 바뀐 직후 횡단 판정 전 수신이 잠깐 끊겨도 대기로 돌아가지 않는다", () => {
  const h = harness();
  for (const t of [0, 250, 500, 750]) h.send("red", t, true);
  for (const t of [1000, 1250, 1500]) h.send("green", t, true);
  for (let t = 1750; t <= 3250; t += 250) h.tick(t);
  for (let t = 3500; t <= 8000; t += 250) h.send("green", t, true);
  assert.equal(h.requests.some(item => item.text === "초록불, 다음 신호까지 대기"), false,
    h.requests.map(item => item.text).join(" / "));
});

test("같은 색은 마지막 안내 후 반복 간격 안에서는 추적 ID가 바뀌어도 다시 읽지 않는다", () => {
  const REPEAT = settings.audio.guidance.traffic_repeat_ms;
  const h = harness();
  for (const t of [0, 250, 500]) h.send("red", t, true);
  h.send(null, 1000, true);
  for (const t of [1250, 1500, 1750]) h.send("red", t, true, { target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불"]);
  for (let t = 2000; t <= REPEAT + 750; t += 250) h.send("red", t, true, { target: 2 });
  assert.deepEqual(h.requests.map(item => item.text), ["빨간불", "빨간불"]);
  assert.equal(h.requests[1].metadata.repeat, true);
});
