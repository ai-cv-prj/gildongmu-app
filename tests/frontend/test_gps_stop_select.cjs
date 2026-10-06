/** 정류장 후보 정리, 안내 대상 전환 보류와 차량별 안내 단계를 화면 없이 검증한다. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const context = { window: {} };
vm.createContext(context);
for (const name of ["gps-motion", "gps-stop-select"]) {
  vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
}
const select = context.window.GStopSelect;
const START = 100000;
const DEG_M = 6371000 * Math.PI / 180;
const latitude = 37.5, longitude = 127;

function point(x, y = 0) {
  return { latitude: latitude + y / DEG_M, longitude: longitude + x / (DEG_M * Math.cos(latitude * Math.PI / 180)) };
}

function sample(second, x, accuracy = 5) {
  return { ...point(x), accuracy, position: { timestamp: START + second * 1000, coords: { ...point(x), accuracy } } };
}

function match(id, x, { order = 1, state = "approaching", vehicle = "v1", text = `${id} 안내` } = {}) {
  return {
    station: { station_id: id, station_name: id, ...point(x), distance_m: Math.abs(x) },
    bus_route_id: "route",
    arrival: { station_order: order, first_arrival: "3분후", first_arrival_state: state, first_vehicle_id: vehicle },
    announcement: text,
  };
}

function tracker(samples) {
  return {
    samples, trackSamples: samples, selectedStationKey: "", selectedStationMatch: null,
    switchCandidate: null, walkingPrediction: null, walkingMotion: { status: "stationary" },
    lastNearbyResult: null, lastNearbyResultAt: 0, walkingVotes: [], walkingConfirmed: false,
  };
}

function choose(state, result, second, options = {}) {
  const chosen = select.selectCandidate(state, result, { now: START + second * 1000, ...options });
  if (chosen) {
    state.selectedStationKey = select.arrivalCandidateKey(chosen);
    state.selectedStationMatch = chosen;
  }
  return chosen;
}

test("안내 후보는 도착정보가 있는 정류장만 중복 없이 가까운 순서로 정렬한다", () => {
  const result = { matches: [match("B", 20), match("A", 5), match("A", 5), match("C", 1, { state: "unavailable" })] };
  const candidates = select.arrivalCandidates(result);
  assert.deepEqual(Array.from(candidates, (item) => item.station.station_id), ["A", "B"]);
  assert.equal(select.arrivalState({ first_arrival: "곧 도착" }), "arriving");
  assert.equal(select.arrivalState({}), "unavailable");
});

test("다른 정류장이 우세해도 서로 다른 측정 3회와 6초가 지나야 전환한다", () => {
  const result = { matches: [match("A", 0), match("B", 20)] };
  const samples = [sample(0, 2)];
  const state = tracker(samples);
  assert.equal(choose(state, result, 0).station.station_id, "A");

  for (const second of [1, 2, 3]) {
    samples.push(sample(second, 17));
    assert.equal(choose(state, result, second).station.station_id, "A");
    assert.equal(state.selectionBasis, "switch_confirmation_pending");
  }
  samples.push(sample(7, 17));
  assert.equal(choose(state, result, 7).station.station_id, "B");
  assert.equal(state.selectionBasis, "recent_distance");
  assert.equal(state.switchCandidate, null);
});

test("전환 조건을 채워도 안내 음성이 재생 중이면 기존 정류장을 유지한다", () => {
  const result = { matches: [match("A", 0), match("B", 20)] };
  const samples = [sample(0, 2)];
  const state = tracker(samples);
  choose(state, result, 0);
  for (const second of [1, 4, 7]) {
    samples.push(sample(second, 17));
    if (second < 7) choose(state, result, second);
  }
  assert.equal(choose(state, result, 7, { speaking: true }).station.station_id, "A");
  assert.equal(choose(state, result, 7).station.station_id, "B");
});

test("오래된 GPS나 낮은 정확도에서는 전환 근거를 쌓지 않는다", () => {
  const result = { matches: [match("A", 0), match("B", 20)] };
  const samples = [sample(0, 2)];
  const state = tracker(samples);
  choose(state, result, 0);
  samples.push(sample(1, 17, 35));
  assert.equal(choose(state, result, 1).station.station_id, "A");
  assert.equal(state.selectionBasis, "switch_evidence_missing");
  samples.push(sample(2, 17));
  assert.equal(choose(state, result, 20).station.station_id, "A");
  assert.equal(state.switchCandidate, null);
});

test("같은 차량은 단계가 오를 때만 새 안내 대상이 되고 다음 차량은 순번이 바뀐다", () => {
  const stops = new Map();
  const first = select.updateAnnouncementStops(stops, [match("A", 0)], "100");
  assert.equal(first.length, 1);
  assert.equal(first[0].phase, 1);
  first[0].vehicle.announcedPhase = 1;

  const same = select.updateAnnouncementStops(stops, [match("A", 0)], "100");
  assert.equal(same[0].vehicle, first[0].vehicle);
  assert.ok(same[0].vehicle.announcedPhase >= same[0].phase);

  const arriving = select.updateAnnouncementStops(stops, [match("A", 0, { state: "arriving" })], "100");
  assert.equal(arriving[0].vehicle, first[0].vehicle);
  assert.equal(arriving[0].phase, 2);

  // 결과에서 잠시 빠져도 안내 이력은 유지된다.
  assert.equal(select.updateAnnouncementStops(stops, [], "100").length, 0);
  assert.equal(stops.size, 1);
  assert.equal([...stops.values()][0].visible, false);

  const next = select.updateAnnouncementStops(stops, [match("A", 0, { vehicle: "v2" })], "100");
  assert.notEqual(next[0].vehicle, first[0].vehicle);
  assert.equal(next[0].vehicle.sequence, 1);
  assert.equal(next[0].vehicle.announcedPhase, 0);
});

test("보행 추정이 없으면 기록 필드를 비워 둔다", () => {
  const record = select.walkingRecord({ ...tracker([]), walkingMotion: null });
  assert.equal(record.walking_motion, null);
  assert.equal(record.walking_destination, null);
  assert.equal(Object.keys(record).length, 2);
});

function biased(second, x, y, accuracy) {
  const coords = { ...point(x, y), accuracy };
  return { ...coords, position: { timestamp: START + second * 1000, coords } };
}

test("정류장 A에 서 있는데 GPS가 B 쪽으로 치우쳐도 오차 범위 안이면 전환하지 않고 구분 불가로 표시한다", () => {
  // 현장 연세대앞 양방향 정류장처럼 두 정류장이 26m 떨어져 있다.
  const result = { matches: [match("A", 0), match("B", 26)] };
  const samples = [sample(0, 0)];
  const state = tracker(samples);
  assert.equal(choose(state, result, 0).station.station_id, "A");
  assert.equal(state.selectionAmbiguous, false);
  for (let second = 1; second <= 15; second++) {
    samples.push(biased(second, 16, 0, 18));
    assert.equal(choose(state, result, second).station.station_id, "A");
  }
  assert.equal(state.selectionAmbiguous, true);
  assert.deepEqual(Array.from(state.ambiguousStationKeys), [select.arrivalCandidateKey(match("B", 26))]);
  assert.equal(select.walkingRecord({ ...state, walkingMotion: { status: "stationary" } }).station_selection_ambiguous, true);
});

test("정확한 GPS로 반대편 정류장까지 건너가면 전환하고 구분 불가 표시를 해제한다", () => {
  const result = { matches: [match("A", 0), match("B", 26)] };
  const samples = [sample(0, 0)];
  const state = tracker(samples);
  choose(state, result, 0);
  for (let second = 1; second <= 26; second++) {
    samples.push(sample(second, second));
    choose(state, result, second);
  }
  assert.equal(state.selectedStationMatch.station.station_id, "B");
  assert.equal(state.selectionAmbiguous, false);
});

test("보고 오차가 15m를 넘으면 다른 후보가 없어도 조회 후보를 믿을 수 없어 구분 불가로 표시한다", () => {
  const state = tracker([biased(0, 0, 0, 16)]);
  assert.equal(choose(state, { matches: [match("A", 0)] }, 0).station.station_id, "A");
  assert.equal(state.selectionAmbiguous, true);
  assert.equal(state.ambiguousStationKeys.length, 0);
});

test("안내 대상이 바뀔 때만 전환 근거와 함께 이벤트를 남기고 상한을 넘으면 오래된 것부터 버린다", () => {
  const state = tracker([sample(0, 0)]);
  const first = select.selectCandidate(state, { matches: [match("A", 0)] }, { now: START });
  select.commitSelection(state, first, START);
  select.commitSelection(state, first, START + 500);
  const selections = state.events?.filter((event) => event.type === "selection") || [];
  assert.equal(selections.length, 1);
  assert.equal(selections[0].to, select.arrivalCandidateKey(first));
  assert.equal(selections[0].basis, "recent_distance");
  assert.equal(selections[0].ambiguous, false);

  for (let index = 0; index < select.CONFIG.maxEvents + 5; index++) select.logEvent(state, "test", { index }, START);
  assert.equal(state.events.length, select.CONFIG.maxEvents);
  assert.equal(state.events.at(-1).index, select.CONFIG.maxEvents + 4);
  const record = select.trackingRecord(state);
  assert.equal(record.events_dropped, 6);
  assert.equal(record.events.length, select.CONFIG.maxEvents);
});

test("버스 번호 인식 결과는 촬영 시각 기준 3초 안의 같은 노선만 현재 근거로 돌려준다", () => {
  const state = tracker([]);
  assert.equal(select.observeBusDetection(state, { routeNumber: "", capturedAtMs: START }), false);
  assert.equal(select.observeBusDetection(state, { routeNumber: "마포 08", capturedAtMs: START, confidence: 0.8, trackId: 3 }), true);
  for (let frame = 1; frame <= 9; frame++) {
    select.observeBusDetection(state, { routeNumber: "마포08", capturedAtMs: START + frame * 100, confidence: 0.9 });
  }
  // 프레임마다 들어와도 같은 번호는 1초 간격으로만 기록한다.
  assert.equal(state.events.filter((event) => event.type === "bus_detection").length, 1);
  assert.equal(select.recentBusDetection(state, "마포08", START + 1000).captured_at_ms, START + 900);
  assert.equal(select.recentBusDetection(state, "272", START + 1000), null);
  assert.equal(select.recentBusDetection(state, "마포08", START + 4000), null);
});
