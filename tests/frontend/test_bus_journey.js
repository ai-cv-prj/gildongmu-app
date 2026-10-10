const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const match = (station = "station-a", state = "approaching", vehicle = "vehicle-1") => ({
  station: { station_id: station, station_name: station, latitude: 37.5, longitude: 127,
    distance_m: 0 }, bus_route_id: "route-id",
  arrival: { route_id: "route-id", direction: "종점", station_order: 1,
    first_arrival: state === "arriving" ? "곧 도착" : "3분 후",
    first_arrival_state: state, first_vehicle_id: vehicle },
  announcement: `${station} 정류장에 143번 버스가 ${state === "arriving" ? "곧 도착합니다" : "3분 후 도착합니다"}.`,
});
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

function harness({ deferred = false, supplemental = false, watch = true, autoStart = true } = {}) {
  let clock = 100000, nextId = 0, playback = null;
  let reply = { matches: [match()] };
  const watches = new Map(), intervals = new Map(), calls = [], requests = [], spoken = [], events = [], polls = [], statuses = [], cancellations = [];
  const geo = { watchPosition(success, error) {
    const id = ++nextId;
    watches.set(id, { success, error });
    return id;
  }, clearWatch(id) { watches.delete(id); } };
  if (!watch) delete geo.watchPosition;
  if (supplemental) geo.getCurrentPosition = (success, error, options) => polls.push({ success, error, options });
  const api = { nearbyBusArrival(args) {
    calls.push(args);
    if (deferred) return new Promise((resolve, reject) => requests.push({ resolve, reject }));
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
  } };
  const player = { speak(text, deadline, callbacks) {
    playback = callbacks;
    spoken.push({ text, deadline, dynamic: callbacks.dynamic });
    if (autoStart) callbacks.onStart?.();
    return true;
  }, cancel() { if (playback) cancellations.push(playback); playback = null; } };
  const context = { window: { GConfig: { get: () => ({ audio: { crosswalk_max_age_ms: 1500,
    playback_timeout_ms: 15000 } }) } }, navigator: { geolocation: geo }, AbortController,
    performance: { now: () => clock - 100000 },
    setInterval(callback) { const id = ++nextId; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); } };
  vm.createContext(context);
  for (const name of ["gps-motion", "gps-stop-select", "audio_coordinator", "bus-journey"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  const coordinator = context.window.GAudioCoordinator.create({ player, now: () => clock - 100000 });
  coordinator.start();
  const journey = context.window.GBusJourney.create({ api, coordinator, now: () => clock,
    monotonicNow: () => clock - 100000, onEvent: event => events.push(event),
    onStatus: message => statuses.push(message) });
  const position = (options = {}) => ({ timestamp: clock, coords: { latitude: 37.5,
    longitude: 127, accuracy: 5, ...options } });
  return { journey, coordinator, spoken, calls, requests, watches, intervals, events, polls, statuses, cancellations, position,
    now: () => clock, setReply(value) { reply = value; },
    playback: () => playback,
    startPlayback() { if (!autoStart) playback?.onStart?.(); },
    step(ms) { clock += ms; for (const tick of intervals.values()) tick(); coordinator.tick(); },
    tick() { for (const tick of intervals.values()) tick(); },
    locate(options) { [...watches.values()].at(-1).success(position(options)); },
    deny() { [...watches.values()].at(-1).error({ code: 1 }); },
    finish() { const ending = playback; playback = null; ending?.onEnd(); },
    detect({ route = "143", state = "recognized_single", capturedAt = clock, id = 4 } = {}) {
      journey.accept({ captured_at_ms: capturedAt, event: { target_route: route,
        matches: [{ route_number: route, state, track_id: id, token_score: .99 }] } }, capturedAt);
    },
    detectOther({ route = "7011", state = "matched_candidate", capturedAt = clock, id = 9,
      target = "143" } = {}) {
      journey.accept({ captured_at_ms: capturedAt, event: { target_route: target,
        matches: [], recognized_routes: [{ route_number: route, state, track_id: id,
          token_score: .99, is_target: false }] } }, capturedAt);
    },
  };
}

for (const [seconds, eta] of [[30, "30초 후"], [61, "1분 후"], [89, "1분 후"],
  [90, "2분 후"], [149, "2분 후"], [150, "3분 후"]]) {
  test(`773 도착정보 ${seconds}초는 ${eta}로 안내한다`, async () => {
    const app = harness();
    const selected = match();
    selected.arrival.first_eta_seconds = seconds;
    app.setReply({ matches: [selected] });
    app.journey.start("773");
    app.locate();
    await flush();
    assert.equal(app.spoken[0].text, `도착정보. 773번, ${eta}.`);
    app.journey.stop();
  });
}

test("GPS 권한 거절 후 후보는 화면에만 표시하고 확정한 목표 버스를 한 번 안내한다", () => {
  const app = harness();
  app.journey.start("143");
  app.deny();
  assert.equal(app.journey.snapshot().gps.status, "denied");
  assert.equal(app.watches.size, 0);
  app.detect();
  assert.equal(app.spoken.length, 0);
  assert.equal(app.journey.snapshot().ocr.confirmed, false);
  assert.equal(app.journey.snapshot().ocr.message, "143번 버스 인식 중.");
  app.detect();
  app.tick();
  assert.equal(app.spoken.length, 0, "단일 프레임 후보는 음성을 시작하지 않는다");
  assert.equal(app.journey.repeat(), false, "확정하지 않은 번호는 다시 듣기로도 읽지 않는다");
  app.detect({ state: "matched_candidate" });
  assert.equal(app.journey.snapshot().ocr.confirmed, true);
  assert.equal(app.journey.snapshot().ocr.message, "143 목표 버스 확인.");
  assert.equal(app.spoken.length, 1);
  assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
  assert.equal(app.spoken.at(-1).dynamic, true);
  app.finish();
  app.detect({ state: "matched_candidate" });
  assert.equal(app.spoken.length, 1, "같은 차량의 확정 프레임은 중복 안내하지 않는다");
  assert.equal(app.journey.snapshot().gps.status, "denied");
  app.detect({ id: 5, state: "matched_candidate" });
  assert.equal(app.spoken.length, 2);
  app.journey.stop();
});

test("새 목표 차량 확정은 이전 차량 안내를 교체하고 취소된 콜백을 무시한다", () => {
  const app = harness();
  app.journey.start("7011");
  app.detect({ route: "7011", state: "matched_candidate" });
  const previousPlayback = app.playback();
  assert.equal(app.spoken[0].text, "7011 목표 버스 확인.");
  app.detect({ route: "7011", state: "matched_candidate", id: 5 });
  const confirmedPlayback = app.playback();
  assert.notEqual(confirmedPlayback, previousPlayback);
  assert.equal(app.spoken.length, 2);
  assert.equal(app.spoken[1].text, "7011 목표 버스 확인.");
  assert.equal(app.journey.snapshot().ocr.message, "7011 목표 버스 확인.");
  previousPlayback.onEnd();
  previousPlayback.onStart();
  app.detect({ route: "7011", state: "matched_candidate", id: 5 });
  app.tick();
  assert.equal(app.playback(), confirmedPlayback);
  assert.equal(app.spoken.length, 2, "취소된 콜백이 확정 안내를 중복 시작하지 않는다");
  assert.equal(app.events.filter(event => event.type === "bus_ocr_speech_completed").length, 0);
  app.finish();
  app.detect({ route: "7011", state: "matched_candidate", id: 5 });
  app.tick();
  assert.equal(app.spoken.length, 2);
  assert.equal(app.journey.repeat(), true);
  assert.equal(app.spoken.length, 3);
  assert.equal(app.spoken[2].text, "7011 목표 버스 확인.");
  app.finish();
  app.tick();
  assert.equal(app.spoken.length, 3, "다시 듣기도 한 번 안내하고 종료한다");
  app.journey.stop();
});

test("다른 버스는 짧게 한 번 안내하고 반복·추적 ID 변경을 제한한다", () => {
  const app = harness();
  app.journey.start("143");
  app.deny();
  app.detectOther();
  assert.equal(app.spoken.at(-1).text, "다른 버스. 목표 버스를 계속 찾는 중.");
  assert.equal(app.spoken.at(-1).dynamic, true);
  assert.equal(app.journey.snapshot().ocr.status, "other");
  assert.equal(app.journey.snapshot().ocr.routeNumber, "7011");
  assert.equal(app.journey.snapshot().ocr.isTarget, false);
  assert.equal(app.journey.snapshot().ocr.message, "7011번 버스, 다른 노선.");
  app.finish();
  app.detectOther();
  app.detectOther({ id: 10 });
  assert.equal(app.spoken.length, 1, "같은 번호의 추적 ID가 바뀌어도 곧바로 반복하지 않는다");
  assert.equal(app.journey.repeat(), true, "사용자가 요청한 다시 듣기는 허용한다");
  assert.equal(app.spoken.length, 2);
  assert.equal(app.spoken.at(-1).text, "다른 버스. 목표 버스를 계속 찾는 중.");
  app.finish();
  app.step(10000);
  app.detectOther({ id: 11 });
  assert.equal(app.spoken.length, 3);
  app.journey.stop();
});

test("다른 버스 안내 중 목표 버스가 확인되면 목표 버스 안내를 우선한다", () => {
  const app = harness();
  app.journey.start("143");
  app.detectOther();
  app.detect({ state: "matched_candidate" });
  assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
  assert.equal(app.journey.snapshot().ocr.isTarget, true);
  app.finish();
  app.detect();
  assert.equal(app.spoken.length, 2, "같은 차량을 확정한 뒤 후보 안내로 되돌리지 않는다");
  app.journey.stop();
});

test("목표 일치와 다른 버스가 함께 보이면 목표 버스를 먼저 읽는다", () => {
  const app = harness();
  app.journey.start("143");
  app.journey.accept({ event: { target_route: "143",
    matches: [{ route_number: "143", track_id: 4, token_score: .99, state: "matched_candidate" }],
    recognized_routes: [{ route_number: "7011", track_id: 9, token_score: 1,
      state: "matched_candidate", is_target: false }] } }, app.now());
  assert.equal(app.spoken.length, 1);
  assert.equal(app.spoken[0].text, "143 목표 버스 확인.");
  app.journey.stop();
});

test("부분·충돌·단발 번호와 원시 OCR 상자는 다른 버스라고 읽지 않는다", () => {
  const app = harness();
  app.journey.start("143");
  for (const state of ["partial", "conflict", "hold", "pending", "recognized_single"]) {
    app.detectOther({ state });
  }
  app.detectOther({ id: null });
  app.journey.accept({ event: { target_route: "143", matches: [], buses: [
    { track_id: 9, observations: [{ text: "7011", eligible: true, token_score: .99 }] },
  ] }, detections: [{ class_name: "route_number", extra: { text: "7011" } }] }, app.now());
  assert.equal(app.spoken.length, 0);
  assert.equal(app.journey.repeat(), false);
  app.journey.stop();
});

test("다른 번호의 오래된 결과·실패 캐시는 재생하지 않고 일시중지 때 음성을 취소한다", () => {
  const app = harness();
  app.journey.start("143");
  const old = app.now();
  app.step(3001);
  app.detectOther({ capturedAt: old });
  assert.equal(app.spoken.length, 0);
  app.detectOther();
  app.finish();
  app.step(3001);
  assert.equal(app.journey.snapshot().ocr.status, "stale");
  assert.equal(app.journey.repeat(), false);
  app.journey.accept({ status: "error", event: { target_route: "143", matches: [],
    recognized_routes: [{ route_number: "7011", track_id: 10, state: "matched_candidate",
      is_target: false }] } }, app.now());
  assert.equal(app.spoken.length, 1);
  app.journey.pause();
  app.detectOther({ id: 11 });
  assert.equal(app.journey.repeat(), false);
  assert.equal(app.spoken.length, 1);
  app.journey.stop();
});

test("OCR 장애는 GPS 조회와 정류장 도착정보를 중단하지 않는다", async () => {
  const app = harness();
  app.journey.start("143");
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "ready");
  app.finish();
  app.journey.accept({ status: "unavailable", event: { matches: [] } }, app.now());
  assert.equal(app.journey.snapshot().ocr.status, "error");
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.equal(app.watches.size, 1);
  assert.match(app.spoken[0].text, /3분 후/);
  app.journey.stop();
});

for (const code of ["bus_api_unconfigured", "bus_api_error", "network_error"]) {
  test(`도착정보 실패(${code}) 후에도 목표 번호 후보·확정·다시 듣기가 동작한다`, async () => {
    const app = harness();
    app.setReply(Object.assign(new Error("도착정보 조회 실패"), { code }));
    app.journey.start("143");
    app.locate();
    await flush();
    assert.match(app.journey.snapshot().gps.message, /카메라.*계속/);
    app.detect();
    assert.equal(app.journey.snapshot().ocr.status, "candidate");
    assert.equal(app.spoken.length, 0, "API 오류 중에도 후보 번호는 화면에만 표시한다");
    app.detect({ id: 5, state: "matched_candidate" });
    assert.equal(app.journey.snapshot().ocr.confirmed, true);
    assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
    assert.equal(app.journey.snapshot().gps.selected, null);
    app.finish();
    assert.equal(app.journey.repeat(), true);
    assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
    app.journey.stop();
  });
}

for (const [code, message] of [
  ["bus_api_unconfigured", /인증키가 설정되지/],
  ["bus_api_auth_failed", /인증에 실패/],
  ["bus_api_unreachable", /API에 연결하지/],
  ["bus_api_error", /API가 오류를 반환/],
  ["bus_api_http_error", /API 요청에 실패/],
  ["bus_api_invalid_response", /API 응답을 읽지/],
]) {
  test(`도착정보 오류 원인(${code})을 표시하고 원본 오류의 인증키는 표시하지 않는다`, async () => {
    const app = harness();
    app.setReply(Object.assign(new Error("upstream-url?serviceKey=private-key"), { code }));
    app.journey.start("143");
    app.locate();
    await flush();
    assert.match(app.journey.snapshot().gps.message, message);
    assert.doesNotMatch(app.journey.snapshot().gps.message, /private-key|serviceKey/);
    app.journey.stop();
  });
}

test("API 미설정 시 위치 조회만 멈추고 OCR을 유지하며 재시작하면 도착정보를 재확인한다", async () => {
  const app = harness({ supplemental: true });
  app.setReply(Object.assign(new Error("인증키가 설정되지 않았습니다"), { code: "bus_api_unconfigured" }));
  app.journey.start("143");
  const oldWatch = [...app.watches.values()][0];
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "unavailable");
  assert.equal(app.watches.size, 0);
  assert.equal(app.intervals.size, 1, "번호 인식의 유효 시간과 음성 처리는 계속한다");
  const polls = app.polls.length;
  app.step(25000);
  oldWatch.success(app.position());
  app.polls[0].error({ code: 1 });
  await flush();
  assert.equal(app.calls.length, 1);
  assert.equal(app.polls.length, polls);
  assert.equal(app.journey.snapshot().gps.status, "unavailable");
  app.detect({ state: "matched_candidate" });
  assert.equal(app.journey.snapshot().ocr.confirmed, true);
  app.setReply({ matches: [match()] });
  app.journey.start("143");
  app.locate();
  await flush();
  assert.equal(app.calls.length, 2);
  assert.equal(app.journey.snapshot().gps.status, "ready");
  app.journey.stop();
});

test("촬영 시각이 없는 OCR 준비·모델 오류도 도착정보 상태와 구분해 표시한다", () => {
  const app = harness();
  app.journey.start("143");
  app.deny();
  app.journey.accept({ status: "loading", event: null, captured_at_ms: null });
  assert.equal(app.journey.snapshot().ocr.status, "loading");
  app.journey.accept({ status: "unavailable", event: null, captured_at_ms: null });
  assert.equal(app.journey.snapshot().ocr.status, "error");
  assert.match(app.journey.snapshot().ocr.message, /모델 파일/);
  app.journey.accept({ status: "error", error: "bus_model_load_failed:RuntimeError", event: null, captured_at_ms: null });
  assert.match(app.journey.snapshot().ocr.message, /모델.*불러오지/);
  assert.equal(app.journey.snapshot().gps.status, "denied");
  app.detect({ state: "matched_candidate" });
  assert.equal(app.journey.snapshot().ocr.confirmed, true);
  app.journey.stop();
});

test("번호가 안 읽힌 한 프레임은 최근 번호 안내를 자르지 않으며 3초가 지나면 지운다", () => {
  const app = harness();
  app.journey.start("143");
  app.detect({ state: "matched_candidate" });
  app.step(500);
  app.journey.accept({ event: { matches: [] } }, app.now());
  assert.equal(app.journey.snapshot().ocr.confirmed, true);
  app.finish();
  app.detect({ state: "matched_candidate" });
  assert.equal(app.spoken.length, 1);
  app.step(3001);
  assert.equal(app.journey.snapshot().ocr.status, "stale");
  assert.equal(app.journey.snapshot().ocr.confirmed, false);
  app.journey.stop();
});

test("노선 변경은 이전 요청을 취소하고 늦은 응답 및 GPS 콜백을 무시한다", async () => {
  const app = harness({ deferred: true });
  app.journey.start("143");
  const oldWatch = [...app.watches.values()][0];
  app.locate();
  assert.equal(app.calls.length, 1);
  app.journey.start("271");
  assert.equal(app.calls[0].signal.aborted, true);
  oldWatch.success(app.position());
  app.requests[0].resolve({ matches: [match("old-stop")] });
  await flush();
  assert.equal(app.journey.snapshot().route, "271");
  assert.equal(app.journey.snapshot().gps.selected, null);
  assert.equal(app.calls.length, 1);
  app.detect({ route: "143" });
  assert.equal(app.spoken.length, 0);
  app.locate();
  assert.equal(app.calls.at(-1).busNumber, "271");
  app.requests[1].resolve({ matches: [match("new-stop")] });
  await flush();
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "new-stop");
  app.journey.stop();
});

test("일시중지와 재개는 이전 위치·카메라 결과를 재사용하지 않고 모든 자원을 정리한다", async () => {
  const app = harness({ deferred: true });
  app.journey.start("143");
  app.locate();
  const beforePause = app.now();
  app.journey.pause();
  assert.equal(app.calls[0].signal.aborted, true);
  assert.equal(app.watches.size, 0);
  assert.equal(app.intervals.size, 0);
  app.step(500);
  app.journey.resume();
  app.detect({ capturedAt: beforePause });
  app.requests[0].resolve({ matches: [match()] });
  await flush();
  assert.equal(app.spoken.length, 0);
  assert.equal(app.journey.snapshot().gps.selected, null);
  assert.equal(app.journey.snapshot().ocr.status, "searching");
  app.detect({ state: "matched_candidate" });
  assert.equal(app.spoken.length, 1);
  app.journey.stop();
  assert.equal(app.watches.size, 0);
  assert.equal(app.intervals.size, 0);
});

test("10초가 지난 GPS와 3초가 지난 OCR 결과는 안내·다시 듣기에 사용하지 않는다", async () => {
  const app = harness();
  app.journey.start("143");
  const capturedAt = app.now();
  app.locate();
  await flush();
  app.finish();
  app.step(3001);
  app.detect({ capturedAt });
  assert.equal(app.journey.snapshot().ocr.status, "stale");
  assert.equal(app.spoken.length, 1);
  app.step(7000);
  assert.equal(app.journey.snapshot().gps.status, "stale");
  assert.equal(app.journey.repeat(), false);
  app.detect({ state: "matched_candidate" });
  assert.equal(app.spoken.length, 2);
  assert.equal(app.journey.snapshot().gps.status, "stale");
  app.journey.stop();
});

test("긴급·신호 안내가 버스 음성보다 우선하며 버스 안내는 최신일 때만 재시도한다", () => {
  const app = harness();
  app.journey.start("143");
  app.coordinator.request({ source: "walking", priority: app.coordinator.PRIORITY.emergency,
    text: "멈추세요.", validUntil: 15000 });
  app.detect({ state: "matched_candidate" });
  assert.deepEqual(app.spoken.map(item => item.text), ["멈추세요."]);
  app.finish();
  app.step(1000);
  assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
  app.coordinator.request({ source: "traffic", priority: app.coordinator.PRIORITY.trafficRed,
    text: "빨간불", validUntil: 15000 });
  assert.equal(app.spoken.at(-1).text, "빨간불");
  app.step(3001);
  app.finish();
  app.tick();
  assert.equal(app.spoken.at(-1).text, "빨간불");
  app.journey.stop();
});

test("목표 버스 음성을 끊은 긴급 안내는 새 목표 차량 확정에도 유지된다", () => {
  const app = harness();
  app.journey.start("143");
  app.detect({ state: "matched_candidate" });
  const previousPlayback = app.playback();
  app.coordinator.request({ source: "walking", priority: app.coordinator.PRIORITY.emergency,
    text: "멈추세요.", validUntil: 15000 });
  const emergencyPlayback = app.playback();
  app.detect({ state: "matched_candidate", id: 5 });
  previousPlayback.onEnd();
  app.tick();
  assert.equal(app.playback(), emergencyPlayback);
  assert.deepEqual(app.spoken.map(item => item.text), ["143 목표 버스 확인.", "멈추세요."]);
  app.finish();
  app.step(1000);
  assert.equal(app.spoken.at(-1).text, "143 목표 버스 확인.");
  app.finish();
  app.tick();
  assert.equal(app.spoken.length, 3);
  app.journey.stop();
});

test("같은 차량의 같은 도착 단계는 반복하지 않고 도착 임박과 새 차량은 다시 안내한다", async () => {
  const app = harness();
  app.journey.start("143");
  app.locate();
  await flush();
  app.finish();
  for (const [phase, vehicle, expectedCount] of [["approaching", "vehicle-1", 1],
    ["arriving", "vehicle-1", 2], ["approaching", "vehicle-2", 3]]) {
    app.step(20000);
    app.setReply({ matches: [match("station-a", phase, vehicle)] });
    app.locate();
    await flush();
    assert.equal(app.spoken.length, expectedCount);
    app.finish();
  }
  app.journey.stop();
});

test("GPS 오차 30m 초과는 조회를 보류하며 일시적 API 오류도 20초 갱신 간격을 지킨다", async () => {
  const app = harness();
  app.journey.start("143");
  app.locate({ accuracy: 31 });
  assert.equal(app.calls.length, 0);
  assert.equal(app.journey.snapshot().gps.status, "inaccurate");
  const error = new Error("도착정보 서버 응답 오류");
  error.code = "bus_api_error";
  app.setReply(error);
  app.locate({ accuracy: 30 });
  await flush();
  assert.equal(app.calls.length, 1);
  assert.match(app.journey.snapshot().gps.message, /카메라 번호 인식은 계속/);
  for (let i = 0; i < 19; i++) { app.step(1000); app.locate(); await flush(); }
  assert.equal(app.calls.length, 1);
  app.step(1000);
  await flush();
  assert.equal(app.calls.length, 2);
  app.journey.stop();
});

test("다른 방향의 정류장을 직접 선택하면 이후 같은 위치 갱신에도 선택을 유지한다", async () => {
  const app = harness();
  app.setReply({ matches: [match("east"), match("west")] });
  app.journey.start("143");
  app.locate();
  await flush();
  app.finish();
  const west = app.journey.snapshot().gps.candidates.find(item => item.station.station_id === "west");
  assert.equal(app.journey.selectStop(west.key), true);
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  app.step(1000);
  app.locate();
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  app.journey.stop();
});

test("일시중지 후 새 GPS로 재개해도 선택한 방향과 완료한 차량 안내 이력을 유지한다", async () => {
  const app = harness();
  app.setReply({ matches: [match("east"), match("west")] });
  app.journey.start("143");
  app.locate();
  await flush();
  app.finish();
  app.journey.selectStop(app.journey.snapshot().gps.candidates[1].key);
  app.finish();
  assert.equal(app.spoken.length, 2);
  app.journey.pause();
  app.step(1000);
  app.journey.resume();
  assert.equal(app.journey.snapshot().gps.selected, null);
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  assert.equal(app.spoken.length, 2);
  app.journey.stop();
});

test("선택한 정류장 ETA가 잠시 빠져도 반대 방향으로 바꾸지 않고 대기한다", async () => {
  const app = harness();
  app.setReply({ matches: [match("east"), match("west")] });
  app.journey.start("143");
  app.locate();
  await flush();
  app.finish();
  app.journey.selectStop(app.journey.snapshot().gps.candidates[1].key);
  app.finish();
  app.step(20000);
  app.setReply({ matches: [match("east", "arriving")] });
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "waiting");
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  assert.equal(app.spoken.length, 2);
  assert.equal(app.journey.repeat(), false);
  app.step(20000);
  app.setReply({ matches: [match("east", "arriving"), match("west", "arriving")] });
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  assert.equal(app.spoken.at(-1).text, "도착정보. 143번, 곧 도착.");
  app.journey.stop();
});

test("ETA 없는 정류장도 직접 선택할 수 있고 새 도착정보가 생기면 그 정류장을 안내한다", async () => {
  const app = harness();
  app.setReply({ matches: [{ ...match("west"), arrival: null, announcement: "도착정보 없음" }] });
  app.journey.start("143");
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.candidates.length, 1);
  assert.equal(app.journey.selectStop(app.journey.snapshot().gps.candidates[0].key), true);
  assert.equal(app.journey.snapshot().gps.status, "waiting");
  app.step(20000);
  app.setReply({ matches: [match("east"), match("west")] });
  app.locate();
  await flush();
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "west");
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.equal(app.spoken.length, 1);
  assert.equal(app.spoken[0].text, "도착정보. 143번, 3분 후.");
  app.journey.stop();
});

test("지연된 도착 조회는 취소하고 다음 갱신이 복구되며 이전 응답은 사용하지 않는다", async () => {
  const app = harness({ deferred: true });
  app.journey.start("143");
  app.locate();
  app.step(10001);
  assert.equal(app.calls[0].signal.aborted, true);
  app.locate();
  app.step(20000);
  app.locate();
  assert.equal(app.calls.length, 2);
  app.requests[0].resolve({ matches: [match("old-stop")] });
  await flush();
  assert.equal(app.journey.snapshot().gps.selected, null);
  app.requests[1].resolve({ matches: [match("new-stop")] });
  await flush();
  assert.equal(app.journey.snapshot().gps.selected.station.station_id, "new-stop");
  app.journey.stop();
});

test("다시 듣기는 최신 OCR 문장을 처음부터 재생하고 오류로 표기된 캐시 번호는 안내하지 않는다", () => {
  const app = harness();
  app.journey.start("143");
  app.detect({ state: "matched_candidate" });
  assert.equal(app.journey.repeat(), true);
  assert.equal(app.spoken.length, 2);
  app.finish();
  app.journey.accept({ status: "error", event: { matches: [{ route_number: "143", track_id: 5,
    state: "matched_candidate" }] } }, app.now());
  assert.equal(app.journey.snapshot().ocr.status, "error");
  assert.equal(app.spoken.length, 2);
  app.journey.stop();
});

test("정류장에서 watch 갱신이 멈춰도 5초 간격 보조 GPS 조회로 도착정보를 계속 갱신한다", async () => {
  const app = harness({ supplemental: true });
  app.journey.start("143");
  assert.equal(app.polls.length, 1);
  assert.equal(app.polls[0].options.maximumAge, 0);
  assert.equal(app.polls[0].options.timeout, 4000);
  app.polls[0].success(app.position());
  await flush();
  app.finish();
  for (let i = 0; i < 6; i++) {
    app.step(5000);
    app.polls.at(-1).success(app.position());
    await flush();
    assert.equal(app.journey.snapshot().gps.status, "ready");
  }
  assert.equal(app.polls.length, 7);
  assert.equal(app.calls.length, 2);
  app.journey.stop();
});

test("보조 GPS는 한 요청만 기다리며 일시중지 전 늦은 콜백을 재개 후 무시한다", async () => {
  const app = harness({ supplemental: true });
  app.journey.start("143");
  const old = app.polls[0];
  for (let i = 0; i < 4; i++) app.step(1000);
  assert.equal(app.polls.length, 1);
  app.journey.pause();
  app.step(1000);
  app.journey.resume();
  assert.equal(app.polls.length, 2);
  old.success(app.position());
  old.error({ code: 1 });
  await flush();
  assert.equal(app.calls.length, 0);
  assert.equal(app.journey.snapshot().gps.status, "locating");
  app.polls[1].success(app.position());
  await flush();
  assert.equal(app.calls.length, 1);
  assert.equal(app.journey.snapshot().gps.status, "ready");
  app.journey.stop();
});

test("watch 미지원 환경은 보조 GPS로 시작하고 GPS 오류 복구를 상단 상태에도 알린다", async () => {
  const app = harness({ supplemental: true, watch: false });
  app.journey.start("143");
  app.polls[0].error({ code: 2 });
  assert.equal(app.journey.snapshot().gps.status, "error");
  app.step(5000);
  app.polls[1].success(app.position());
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.match(app.statuses.at(-1), /현재 위치와 정류장을 확인했습니다/);
  app.step(5000);
  app.polls[2].error({ code: 3 });
  assert.equal(app.journey.snapshot().gps.status, "ready", "최근 정상 위치가 있으면 보조 조회 시간 초과로 지우지 않는다");
  app.journey.stop();
});

for (const autoStart of [true, false]) {
  test(`773번 확정은 ${autoStart ? "재생 중인" : "재생 준비 중인"} 도착정보를 중단하고 번호 확인을 안내한다`, async () => {
    const app = harness({ autoStart });
    app.journey.start("773");
    app.locate();
    await flush();
    const arrivalPlayback = app.playback();
    assert.equal(app.spoken[0].text, "도착정보. 773번, 3분 후.");
    app.detect({ route: "773", state: "matched_candidate" });
    const targetPlayback = app.playback();
    assert.notEqual(targetPlayback, arrivalPlayback);
    assert.deepEqual(app.cancellations, [arrivalPlayback]);
    assert.deepEqual(app.spoken.map(item => item.text), [
      "도착정보. 773번, 3분 후.", "773 목표 버스 확인.",
    ]);
    app.startPlayback();
    arrivalPlayback.onEnd();
    arrivalPlayback.onStart();
    app.tick();
    assert.equal(app.playback(), targetPlayback);
    assert.equal(app.events.filter(event => event.type === "bus_arrival_speech_completed").length, 0);
    app.finish();
    app.tick();
    assert.equal(app.spoken.length, 2, "목표 버스가 최신이면 이전 도착 문장을 재시작하지 않는다");
    app.journey.stop();
  });
}

test("773번 도착정보 중 7011번 인식은 현재 문장과 이후 도착 단계 안내를 유지한다", async () => {
  const app = harness();
  app.journey.start("773");
  app.locate();
  await flush();
  const arrivalPlayback = app.playback();
  app.detectOther({ target: "773" });
  assert.equal(app.journey.snapshot().ocr.routeNumber, "7011");
  assert.equal(app.playback(), arrivalPlayback);
  assert.equal(app.cancellations.length, 0);
  assert.deepEqual(app.spoken.map(item => item.text), ["도착정보. 773번, 3분 후."]);
  app.finish();
  app.tick();
  assert.equal(app.spoken.length, 1, "도착 문장이 끝나도 다른 노선 음성으로 전환하지 않는다");
  app.step(1000);
  app.setReply({ matches: [match("station-a", "arriving")] });
  app.locate({ latitude: 37.50014 });
  await flush();
  assert.equal(app.journey.snapshot().ocr.status, "other");
  assert.equal(app.spoken.at(-1).text, "도착정보. 773번, 곧 도착.");
  assert.equal(app.spoken.length, 2);
  app.journey.stop();
});

for (const autoStart of [true, false]) {
  test(`GPS ${autoStart ? "재생" : "준비"} 중 생략한 다른 버스는 GPS 만료 후 지연 재생하지 않는다`, async () => {
    const app = harness({ autoStart });
    app.journey.start("773");
    app.locate();
    await flush();
    app.step(9000);
    const suppressedCaptureAt = app.now();
    app.detectOther({ target: "773" });
    assert.equal(app.spoken.length, 1);
    app.startPlayback();
    app.finish();
    app.step(1001);
    assert.equal(app.journey.snapshot().gps.status, "stale");
    assert.equal(app.journey.snapshot().ocr.status, "other", "최근 인식은 화면에 유지한다");
    app.tick();
    assert.equal(app.spoken.length, 1, "생략한 인식을 GPS 종료·만료 뒤 자동 재생하지 않는다");
    app.detectOther({ target: "773", capturedAt: suppressedCaptureAt });
    assert.equal(app.spoken.length, 1, "같은 촬영 결과를 캐시에서 다시 받아도 지연 재생하지 않는다");
    app.detectOther({ target: "773", capturedAt: suppressedCaptureAt, id: 10 });
    assert.equal(app.spoken.length, 1, "추적 ID가 바뀌어도 생략한 촬영 결과는 자동 재생하지 않는다");
    app.detectOther({ target: "773" });
    assert.equal(app.spoken.length, 2, "GPS 없이 새로 확인한 관측은 안내한다");
    assert.equal(app.spoken.at(-1).text, "다른 버스. 목표 버스를 계속 찾는 중.");
    app.journey.stop();
  });
}

test("773번 도착 음성이 준비 중일 때 다른 노선을 인식해도 원래 재생 요청을 유지한다", async () => {
  const app = harness({ autoStart: false });
  app.journey.start("773");
  app.locate();
  await flush();
  const arrivalPlayback = app.playback();
  app.detectOther({ target: "773" });
  app.tick();
  assert.equal(app.playback(), arrivalPlayback);
  assert.equal(app.cancellations.length, 0);
  assert.equal(app.spoken.length, 1);
  app.startPlayback();
  app.finish();
  app.tick();
  assert.equal(app.spoken.length, 1);
  app.journey.stop();
});

test("773번 후보 인식 후 늦게 온 GPS 결과도 도착정보를 정상 안내한다", async () => {
  const app = harness({ deferred: true });
  app.journey.start("773");
  app.locate();
  app.detect({ route: "773" });
  assert.equal(app.journey.snapshot().ocr.status, "candidate");
  assert.equal(app.spoken.length, 0);
  app.requests[0].resolve({ matches: [match()] });
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.deepEqual(app.spoken.map(item => item.text), ["도착정보. 773번, 3분 후."]);
  app.journey.stop();
});

test("773번 후보가 최신이어도 새 도착 단계 안내를 막지 않는다", async () => {
  const app = harness();
  app.journey.start("773");
  app.locate();
  await flush();
  app.finish();
  app.step(1000);
  app.detect({ route: "773" });
  app.setReply({ matches: [match("station-a", "arriving")] });
  app.locate({ latitude: 37.50014 });
  await flush();
  assert.equal(app.journey.snapshot().ocr.status, "candidate");
  assert.deepEqual(app.spoken.map(item => item.text), [
    "도착정보. 773번, 3분 후.", "도착정보. 773번, 곧 도착.",
  ]);
  app.journey.stop();
});

for (const evidence of ["other", "candidate"]) {
  test(`다시 듣기는 ${evidence === "other" ? "다른 노선" : "미확정 목표 번호"}보다 773번 도착정보를 안내한다`, async () => {
    const app = harness();
    app.journey.start("773");
    app.locate();
    await flush();
    const arrivalPlayback = app.playback();
    if (evidence === "other") app.detectOther({ target: "773" });
    else app.detect({ route: "773" });
    assert.equal(app.journey.repeat(), true);
    assert.notEqual(app.playback(), arrivalPlayback);
    assert.deepEqual(app.cancellations, [arrivalPlayback]);
    assert.deepEqual(app.spoken.map(item => item.text), [
      "도착정보. 773번, 3분 후.", "도착정보. 773번, 3분 후.",
    ]);
    app.finish();
    app.tick();
    assert.equal(app.spoken.length, 2);
    app.journey.stop();
  });
}

test("다시 듣기는 최신 773번 확정이 있으면 GPS보다 목표 번호 확인을 안내한다", async () => {
  const app = harness();
  app.journey.start("773");
  app.locate();
  await flush();
  app.detect({ route: "773", state: "matched_candidate" });
  app.finish();
  assert.equal(app.journey.repeat(), true);
  assert.deepEqual(app.spoken.map(item => item.text), [
    "도착정보. 773번, 3분 후.", "773 목표 버스 확인.", "773 목표 버스 확인.",
  ]);
  app.journey.stop();
});

test("773번 확정은 음성이 끝나도 최신인 동안 새 GPS 도착 단계 안내를 보류한다", async () => {
  const app = harness();
  app.journey.start("773");
  app.locate();
  await flush();
  app.detect({ route: "773", state: "matched_candidate" });
  app.finish();
  app.step(1000);
  app.setReply({ matches: [match("station-a", "arriving")] });
  app.locate({ latitude: 37.50014 });
  await flush();
  assert.equal(app.journey.snapshot().gps.selected.arrival.first_arrival_state, "arriving");
  assert.equal(app.spoken.length, 2);
  app.journey.stop();
});

test("773번 확정이 오래되면 보류한 최신 GPS 도착정보를 다시 안내한다", async () => {
  const app = harness({ deferred: true });
  app.journey.start("773");
  app.locate();
  app.detect({ route: "773", state: "matched_candidate" });
  app.requests[0].resolve({ matches: [match()] });
  await flush();
  assert.equal(app.journey.snapshot().gps.status, "ready");
  assert.deepEqual(app.spoken.map(item => item.text), ["773 목표 버스 확인."]);
  app.finish();
  app.step(3001);
  assert.equal(app.journey.snapshot().ocr.status, "stale");
  assert.deepEqual(app.spoken.map(item => item.text), [
    "773 목표 버스 확인.", "도착정보. 773번, 3분 후.",
  ]);
  app.journey.stop();
});

for (const autoStart of [true, false]) {
  test(`이전에 안내한 773번 차량을 다시 확인하면 ${autoStart ? "재생 중인" : "재생 준비 중인"} GPS를 목표 번호 안내로 교체한다`, async () => {
    const app = harness({ autoStart });
    app.journey.start("773");
    app.detect({ route: "773", state: "matched_candidate" });
    app.startPlayback();
    app.finish();
    app.step(3001);
    app.locate();
    await flush();
    const arrivalPlayback = app.playback();
    assert.equal(app.spoken.at(-1).text, "도착정보. 773번, 3분 후.");
    app.detect({ route: "773", state: "matched_candidate" });
    const targetPlayback = app.playback();
    assert.notEqual(targetPlayback, arrivalPlayback);
    assert.deepEqual(app.cancellations, [arrivalPlayback]);
    assert.deepEqual(app.spoken.map(item => item.text), [
      "773 목표 버스 확인.", "도착정보. 773번, 3분 후.", "773 목표 버스 확인.",
    ]);
    app.startPlayback();
    arrivalPlayback.onStart();
    arrivalPlayback.onEnd();
    app.detect({ route: "773", state: "matched_candidate" });
    app.tick();
    assert.equal(app.playback(), targetPlayback);
    assert.equal(app.spoken.length, 3, "같은 차량의 뒤따르는 프레임은 확인 음성을 중복 시작하지 않는다");
    app.finish();
    app.detect({ route: "773", state: "matched_candidate" });
    app.tick();
    assert.equal(app.spoken.length, 3);
    app.journey.stop();
  });
}

for (const autoStart of [true, false]) {
  test(`GPS가 복구되면 ${autoStart ? "재생 중인" : "재생 준비 중인"} 다른 노선 안내를 같은 단계의 773번 도착정보로 교체한다`, async () => {
    const app = harness({ autoStart });
    app.journey.start("773");
    app.locate();
    await flush();
    app.startPlayback();
    app.finish();
    app.step(10001);
    assert.equal(app.journey.snapshot().gps.status, "stale");
    app.detectOther({ target: "773" });
    const otherPlayback = app.playback();
    assert.equal(app.spoken.at(-1).text, "다른 버스. 목표 버스를 계속 찾는 중.");
    app.locate();
    await flush();
    const arrivalPlayback = app.playback();
    assert.equal(app.journey.snapshot().gps.status, "ready");
    assert.notEqual(arrivalPlayback, otherPlayback);
    assert.deepEqual(app.cancellations, [otherPlayback]);
    assert.deepEqual(app.spoken.map(item => item.text), [
      "도착정보. 773번, 3분 후.", "다른 버스. 목표 버스를 계속 찾는 중.",
      "도착정보. 773번, 3분 후.",
    ]);
    app.startPlayback();
    otherPlayback.onStart();
    otherPlayback.onEnd();
    app.locate();
    app.tick();
    assert.equal(app.playback(), arrivalPlayback);
    assert.equal(app.spoken.length, 3);
    app.finish();
    app.locate();
    app.tick();
    assert.equal(app.spoken.length, 3, "복구 후 같은 GPS 단계는 반복하지 않는다");
    app.journey.stop();
  });
}
