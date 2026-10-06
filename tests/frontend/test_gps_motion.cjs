/** GPS 흔들림, 오래된 좌표, 회전과 정류장 접근 추정의 경계를 검증한다. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const context = { window: {} };
vm.runInNewContext(fs.readFileSync("frontend/js/gps-motion.js", "utf8"), context);
const { estimate, predictStop } = context.window.GGpsMotion;
const START = 100000;
const DEG_M = 6371000 * Math.PI / 180;
const latitude = 37.5, longitude = 127;

function point(x, y = 0) {
  return { latitude: latitude + y / DEG_M, longitude: longitude + x / (DEG_M * Math.cos(latitude * Math.PI / 180)) };
}

function sample(second, x, y = 0, accuracy = 5, extraCoords = {}) {
  const coords = { ...point(x, y), accuracy, ...extraCoords };
  return { ...point(x, y), accuracy, position: { timestamp: START + second * 1000, coords } };
}

function walk({ seconds = 20, speed = 0.7, accuracy = 5, heading = 90 } = {}) {
  return Array.from({ length: seconds + 1 }, (_, second) => sample(second,
    second * speed * Math.sin(heading * Math.PI / 180), second * speed * Math.cos(heading * Math.PI / 180), accuracy));
}

function stop(key, x, y, currentX = 14, currentY = 0) {
  return { key, ...point(x, y), distanceM: Math.hypot(x - currentX, y - currentY) };
}

test("0.7m/s 보행은 10초보다 오래된 궤적도 이용해 동쪽으로 추정한다", () => {
  const motion = estimate(walk(), START + 20000);
  assert.equal(motion.status, "moving");
  assert.equal(motion.directionLabel, "동쪽");
  assert.ok(Math.abs(motion.headingDeg - 90) < 1);
  assert.ok(Math.abs(motion.speedMps - 0.7) < 0.01);
  assert.ok(motion.spanMs >= 10000);
  assert.ok(motion.trajectory[0].timestampMs < START + 10000);
  assert.doesNotThrow(() => JSON.parse(JSON.stringify(motion)));
});

test("GPS 정확도가 낮으면 같은 거리의 보행 방향을 확정하지 않는다", () => {
  const motion = estimate(walk({ seconds: 12, accuracy: 20 }), START + 12000);
  assert.equal(motion.status, "uncertain");
  assert.equal(motion.headingDeg, null);
});

test("더 긴 관측으로 정확도 10m의 느린 보행도 감지한다", () => {
  assert.equal(estimate(walk({ seconds: 30, accuracy: 10 }), START + 30000).status, "moving");
});

test("고주파 좌표도 관측 범위를 유지하고 회귀 표본은 64개로 제한한다", () => {
  const samples = Array.from({ length: 601 }, (_, index) => sample(index / 20, index / 20 * 0.7, 0, 10));
  const motion = estimate(samples, START + 30000);
  assert.equal(motion.status, "moving");
  assert.ok(motion.spanMs >= 20000);
  assert.ok(motion.sampleCount <= 64);
});

test("정지 중 GPS 흔들림은 보행으로 표시하지 않는다", () => {
  const samples = Array.from({ length: 25 }, (_, second) => sample(second,
    [1, -1, 0, 1, 0][second % 5], [0, 1, -1, 0][second % 4]));
  const motion = estimate(samples, START + 24000);
  assert.equal(motion.status, "stationary");
  assert.equal(motion.headingDeg, null);
  assert.equal(predictStop(motion, [stop("A", 20, 0)]).key, null);
});

test("기기 나침반 방향과 속도만으로 이동 방향을 생성하지 않는다", () => {
  const samples = Array.from({ length: 20 }, (_, second) => sample(second, 0, 0, 5, { heading: 90, speed: 1.2 }));
  assert.equal(estimate(samples, START + 19000).headingDeg, null);
  const walking = walk().map((item) => ({ ...item, position: { ...item.position,
    coords: { ...item.position.coords, heading: 270, speed: NaN } } }));
  assert.equal(estimate(walking, START + 20000).directionLabel, "동쪽");
});

test("중간 GPS 한 점의 큰 튐은 나머지 정상 궤적을 버리지 않는다", () => {
  const samples = walk();
  samples[15] = sample(15, 110, -70);
  const motion = estimate(samples, START + 20000);
  assert.equal(motion.status, "moving");
  assert.equal(motion.directionLabel, "동쪽");
  assert.ok(!motion.trajectory.some((item) => item.timestampMs === START + 15000));
});

test("마지막 GPS 점이 튀면 이전 이동 방향으로 목적 정류장을 제안하지 않는다", () => {
  const samples = walk();
  samples[20] = sample(20, 100, 100);
  const motion = estimate(samples, START + 20000);
  assert.equal(motion.status, "uncertain");
  assert.equal(motion.reason, "gps_jump");
  assert.equal(predictStop(motion, [stop("A", 28, 0)]).key, null);
});

test("최근 좌표가 10초를 넘기면 이동 방향이 만료된다", () => {
  const samples = walk();
  assert.equal(estimate(samples, START + 30000).status, "moving");
  assert.equal(estimate(samples, START + 30001).status, "stale");
});

test("미래와 비정상 좌표, 정확도 30m 초과 표본을 거르고 같은 시각을 중복 계산하지 않는다", () => {
  const samples = walk();
  samples.push(sample(90, 100));
  samples.splice(10, 0, { ...sample(10, 0), latitude: NaN });
  samples.splice(5, 0, sample(5, -20, 0, 31));
  const motion = estimate([...samples, ...samples], START + 20000);
  assert.equal(motion.status, "moving");
  assert.equal(new Set(motion.trajectory.map((item) => item.timestampMs)).size, motion.sampleCount);
  assert.equal(estimate(Array(10).fill(sample(0, 0)), START).status, "uncertain");
  assert.equal(estimate([sample(90, 100)], START).status, "uncertain");
  assert.equal(estimate([...walk(), sample(21, 15, 0, 31)], START + 21000).status, "uncertain");
});

test("30초 전에만 움직였다면 오래된 궤적으로 현재 방향을 생성하지 않는다", () => {
  const samples = [...walk(), sample(60, 30)];
  assert.equal(estimate(samples, START + 60000).status, "uncertain");
});

test("걷다가 멈추면 이전 방향을 지운다", () => {
  const samples = [...walk(), ...Array.from({ length: 10 }, (_, index) => sample(21 + index, 14))];
  const motion = estimate(samples, START + 30000);
  assert.equal(motion.status, "stationary");
  assert.equal(motion.headingDeg, null);
});

test("3초 간격 GPS에서도 정지 궤적을 이전 보행보다 우선한다", () => {
  for (const walkingUntil of [21, 30]) {
    const samples = Array.from({ length: walkingUntil / 3 + 1 }, (_, index) => sample(index * 3, index * 3 * 0.7));
    const walking = estimate(samples, START + walkingUntil * 1000);
    assert.equal(walking.status, "moving");
    assert.equal(walking.directionLabel, "동쪽");
    for (const elapsed of [3, 6, 9, 12]) {
      samples.push(sample(walkingUntil + elapsed, walkingUntil * 0.7));
      if (elapsed < 9) continue;
      const stopped = estimate(samples, START + (walkingUntil + elapsed) * 1000);
      assert.equal(stopped.status, "stationary", `${walkingUntil}초 보행 뒤 ${elapsed}초 정지`);
      assert.equal(stopped.headingDeg, null);
      assert.equal(stopped.speedMps, 0);
      assert.equal(predictStop(stopped, [stop("ahead", walkingUntil * 0.7 + 15, 0, walkingUntil * 0.7)]).key, null);
    }
  }
});

test("3초 간격의 처음 세 표본으로도 6초 동안의 정지를 확인한다", () => {
  const motion = estimate([sample(0, 0), sample(3, 0), sample(6, 0)], START + 6000);
  assert.equal(motion.status, "stationary");
  assert.equal(motion.headingDeg, null);
});

test("방향 전환 중에는 유보하고 충분한 새 궤적이 생기면 새 방향을 따른다", () => {
  const samples = walk();
  for (let i = 1; i <= 6; i++) samples.push(sample(20 + i, 14 - i * 0.7));
  const turning = estimate(samples, START + 26000);
  assert.equal(turning.status, "uncertain");
  for (let i = 7; i <= 16; i++) samples.push(sample(20 + i, 14 - i * 0.7));
  const turned = estimate(samples, START + 36000);
  assert.equal(turned.status, "moving");
  assert.equal(turned.directionLabel, "서쪽");
});

test("90도 회전 뒤에는 이전 대각선 평균을 제시하지 않는다", () => {
  const samples = walk();
  for (let i = 1; i <= 6; i++) samples.push(sample(20 + i, 14, i * 0.7));
  assert.equal(estimate(samples, START + 26000).status, "uncertain");
  for (let i = 7; i <= 16; i++) samples.push(sample(20 + i, 14, i * 0.7));
  assert.equal(estimate(samples, START + 36000).directionLabel, "북쪽");
});

test("앞쪽으로 가까워지는 정류장을 뒤쪽의 더 가까운 정류장보다 우선한다", () => {
  const motion = estimate(walk(), START + 20000);
  const prediction = predictStop(motion, [stop("behind", 7, 0), stop("ahead", 29, 0)]);
  assert.equal(prediction.key, "ahead");
  assert.equal(prediction.reason, "approaching_stop");
  assert.ok(prediction.scores[0].approachM > 0);
});

test("앞쪽이라도 궤적에서 거리가 감소하지 않으면 제안하지 않는다", () => {
  const motion = estimate(walk(), START + 20000);
  // 직렬화된 입력이 불일치해도 방향 값 하나만으로 선택하지 않는다.
  motion.trajectory = [...motion.trajectory].reverse().map((item, index) => ({
    ...item, timestampMs: START + index * 1000,
  }));
  assert.equal(predictStop(motion, [stop("ahead", 29, 0)]).key, null);
});

test("거의 같은 방향에 있는 정류장들의 탑승 의도를 추측하지 않는다", () => {
  const motion = estimate(walk(), START + 20000);
  assert.equal(predictStop(motion, [stop("A", 28, 0), stop("B", 35, 2)]).reason, "ambiguous_stops");
  assert.equal(predictStop(motion, [stop("A", 28, 4), stop("B", 28, -4)]).key, null);
});

test("각도와 접근 속도에서 뚜렷한 차이가 있으면 맞는 후보를 제안한다", () => {
  const motion = estimate(walk(), START + 20000);
  assert.equal(predictStop(motion, [stop("A", 29, 0), stop("B", 25, 15)]).key, "A");
});

test("이미 도착했거나 지나친 정류장, 30m 밖의 후보는 제안하지 않는다", () => {
  const motion = estimate(walk(), START + 20000);
  assert.equal(predictStop(motion, [stop("at", 16, 0)]).reason, "at_stop");
  assert.equal(predictStop(motion, [stop("passed", 5, 0)]).key, null);
  assert.equal(predictStop(motion, [stop("far", 45, 0)]).reason, "no_nearby_stop");
  assert.equal(predictStop(motion, [{ ...stop("far", 45, 0), distanceM: 20 }]).key, null);
});

test("계산이 입력을 바꾸지 않고 결과를 JSON으로 보관할 수 있다", () => {
  const samples = walk();
  const original = JSON.stringify(samples);
  const motion = estimate(samples, START + 20000);
  const candidates = [stop("ahead", 29, 0)];
  const originalCandidates = JSON.stringify(candidates);
  const prediction = predictStop(motion, candidates);
  assert.equal(JSON.stringify(samples), original);
  assert.equal(JSON.stringify(candidates), originalCandidates);
  assert.equal(JSON.parse(JSON.stringify(prediction)).key, "ahead");
});
