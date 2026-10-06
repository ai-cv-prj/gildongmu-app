/** GPS 궤적에서 보행 방향과 접근 중인 정류장 후보를 보수적으로 추정한다. */
(() => {
  "use strict";

  const EARTH_M = 6371000;
  const RAD = Math.PI / 180;
  const MAX_AGE_MS = 10000;
  const HISTORY_MS = 30000;
  const MAX_ACCURACY_M = 30;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const validPoint = (point) => point && finite(point.latitude) && finite(point.longitude)
    && Math.abs(point.latitude) <= 90 && Math.abs(point.longitude) <= 180;
  const median = (values) => percentile(values, 0.5);

  function percentile(values, fraction) {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    const at = (sorted.length - 1) * fraction;
    return sorted[Math.floor(at)] * (1 - at % 1) + sorted[Math.ceil(at)] * (at % 1);
  }

  function offset(origin, point) {
    const longitudeDelta = ((point.longitude - origin.longitude + 540) % 360) - 180;
    return {
      x: longitudeDelta * RAD * EARTH_M * Math.cos((point.latitude + origin.latitude) * RAD / 2),
      y: (point.latitude - origin.latitude) * RAD * EARTH_M,
    };
  }

  function distance(first, second) {
    const delta = offset(first, second);
    return Math.hypot(delta.x, delta.y);
  }

  const heading = (x, y) => (Math.atan2(x, y) / RAD + 360) % 360;
  const angleDifference = (first, second) => Math.abs(((first - second + 540) % 360) - 180);

  // 짧은 간격의 흔들림과 표본 하나의 큰 오차가 기울기를 지배하지 않게 한다.
  function slopes(points, value, minimumSeconds = 2) {
    const velocities = [];
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const seconds = (points[j].timestampMs - points[i].timestampMs) / 1000;
        if (seconds >= minimumSeconds) velocities.push((value(points[j]) - value(points[i])) / seconds);
      }
    }
    return velocities;
  }

  function fit(points) {
    const origin = points[0];
    const projected = points.map((point) => ({ ...point, ...offset(origin, point) }));
    const vx = median(slopes(projected, (point) => point.x));
    const vy = median(slopes(projected, (point) => point.y));
    const elapsed = (point) => (point.timestampMs - origin.timestampMs) / 1000;
    const x0 = median(projected.map((point) => point.x - vx * elapsed(point)));
    const y0 = median(projected.map((point) => point.y - vy * elapsed(point)));
    const residuals = projected.map((point) => Math.hypot(
      point.x - x0 - vx * elapsed(point), point.y - y0 - vy * elapsed(point),
    ));
    return {
      speedMps: Math.hypot(vx, vy), headingDeg: heading(vx, vy), residuals,
      displacementM: Math.hypot(vx, vy) * elapsed(points.at(-1)),
    };
  }

  function samplePoint(sample) {
    if (!validPoint(sample) || !finite(sample.accuracy) || sample.accuracy < 0
      || sample.accuracy > MAX_ACCURACY_M) return null;
    return {
      latitude: sample.latitude, longitude: sample.longitude,
      accuracyM: sample.accuracy, timestampMs: sample.position.timestamp,
      // 기기 heading은 이동 방향이나 정류장 의도를 나타내지 않으므로 사용하지 않는다.
      deviceSpeedMps: finite(sample.position?.coords?.speed) && sample.position.coords.speed >= 0
        && sample.position.coords.speed <= 15 ? sample.position.coords.speed : null,
    };
  }

  function suddenJump(first, second) {
    const seconds = (second.timestampMs - first.timestampMs) / 1000;
    return distance(first, second) > Math.max(12, (first.accuracyM + second.accuracyM) * 1.5, seconds * 3.5);
  }

  function removeIsolatedJumps(points) {
    const cleaned = [];
    for (let i = 0; i < points.length; i++) {
      const previous = cleaned.at(-1), current = points[i], next = points[i + 1];
      if (previous && next && suddenJump(previous, current) && suddenJump(current, next)
        && !suddenJump(previous, next)) continue;
      // 첫 표본만 튄 경우에도 이후의 정상 궤적을 버리지 않는다.
      if (i === 0 && next && points[2] && suddenJump(current, next)
        && !suddenJump(next, points[2])) continue;
      cleaned.push(current);
    }
    return cleaned;
  }

  const publicPoint = (point) => ({ latitude: point.latitude, longitude: point.longitude,
    accuracyM: point.accuracyM, timestampMs: point.timestampMs });

  function estimate(samples, nowMs) {
    const base = { status: "uncertain", headingDeg: null, directionLabel: null, speedMps: null,
      reason: "insufficient_samples", timestampMs: null, accuracyM: null, displacementM: null,
      spanMs: 0, sampleCount: 0, latest: null, trajectory: [] };
    if (!finite(nowMs)) return { ...base, reason: "invalid_time" };
    const ordered = (Array.isArray(samples) ? samples : [])
      .filter((sample) => finite(sample?.position?.timestamp) && sample.position.timestamp <= nowMs)
      .sort((a, b) => a.position.timestamp - b.position.timestamp);
    if (!ordered.length) return base;
    const newest = ordered.at(-1);
    if (nowMs - newest.position.timestamp > MAX_AGE_MS) {
      return { ...base, status: "stale", reason: "stale_location", timestampMs: newest.position.timestamp };
    }
    if (!samplePoint(newest)) return { ...base, reason: "inaccurate_location", timestampMs: newest.position.timestamp };

    const distinct = new Map();
    for (const sample of ordered) {
      if (nowMs - sample.position.timestamp > HISTORY_MS) continue;
      const point = samplePoint(sample);
      if (point && !distinct.has(point.timestampMs)) distinct.set(point.timestampMs, point);
    }
    const cleaned = removeIsolatedJumps([...distinct.values()]);
    // 고주파 GPS도 30초 관측 범위를 유지하면서 회귀 계산량을 제한한다.
    const points = cleaned.length <= 64 ? cleaned : Array.from({ length: 64 }, (_, index) =>
      cleaned[Math.round(index * (cleaned.length - 1) / 63)]);
    const latest = points.at(-1);
    if (!latest) return base;
    const context = { ...base, timestampMs: latest.timestampMs, accuracyM: latest.accuracyM,
      latest: publicPoint(latest), sampleCount: points.length,
      spanMs: latest.timestampMs - points[0].timestampMs };
    if (points.length < 3 || context.spanMs < 6000) return context;
    if (suddenJump(points.at(-2), latest)) return { ...context, reason: "gps_jump" };

    const recent = points.filter((point) => latest.timestampMs - point.timestampMs <= 8000);
    const recentSpanMs = latest.timestampMs - recent[0].timestampMs;
    // 3초 간격 GPS에서도 6초에 걸친 세 점으로 정지를 먼저 확인한다.
    if (recent.length >= 3 && recentSpanMs >= 6000) {
      const recentFit = fit(recent);
      const recentAccuracy = median(recent.map((point) => point.accuracyM));
      const speeds = recent.map((point) => point.deviceSpeedMps).filter(finite);
      const stationaryGeometry = recentFit.speedMps <= 0.25
        && percentile(recentFit.residuals, 0.8) <= Math.max(3, recentAccuracy * 0.65);
      const deviceStopped = speeds.length >= 3 && median(speeds) <= 0.2
        && recentFit.displacementM <= Math.max(3, recentAccuracy * 0.5);
      if (stationaryGeometry || deviceStopped) {
        return { ...context, status: "stationary", speedMps: 0, reason: "stationary",
          spanMs: recentSpanMs, displacementM: recentFit.displacementM };
      }
    }
    if (points.length < 4) return context;

    let reason = "movement_within_accuracy";
    // 짧은 구간부터 확인해 이전 진행 방향이 회전 후에도 오래 남지 않게 한다.
    for (const windowMs of [8000, 12000, 18000, 24000, HISTORY_MS]) {
      const segment = points.filter((point) => latest.timestampMs - point.timestampMs <= windowMs);
      const spanMs = latest.timestampMs - segment[0].timestampMs;
      if (segment.length < 4 || spanMs < 6000) continue;
      const accuracyM = Math.max(latest.accuracyM, median(segment.map((point) => point.accuracyM)));
      const motion = fit(segment);
      if (motion.speedMps > 3) { reason = "not_walking"; continue; }
      if (motion.speedMps < 0.35 || motion.displacementM < Math.max(5, accuracyM * 1.5)) continue;
      if (percentile(motion.residuals, 0.8) > Math.max(3, accuracyM * 0.75)
        || motion.residuals.at(-1) > Math.max(3, latest.accuracyM * 0.75)) {
        reason = "inconsistent_trajectory";
        continue;
      }

      const halfTime = segment[0].timestampMs + spanMs / 2;
      const halves = [segment.filter((point) => point.timestampMs <= halfTime),
        segment.filter((point) => point.timestampMs >= halfTime)];
      const tail = segment.filter((point) => latest.timestampMs - point.timestampMs <= 6000);
      const directions = [...halves, tail].filter((part) => part.length >= 3)
        .map(fit).filter((part) => part.speedMps >= 0.35
          && part.displacementM >= Math.max(2.5, accuracyM * 0.6));
      if (directions.some((part) => angleDifference(part.headingDeg, motion.headingDeg) > 50)
        || (directions.length >= 2 && angleDifference(directions[0].headingDeg, directions[1].headingDeg) > 55)) {
        reason = "changing_direction";
        continue;
      }
      const trajectory = segment.filter((point, index) => motion.residuals[index] <= Math.max(4, point.accuracyM * 1.5));
      return {
        ...context, status: "moving", reason: "consistent_walking", headingDeg: motion.headingDeg,
        directionLabel: ["북쪽", "북동쪽", "동쪽", "남동쪽", "남쪽", "남서쪽", "서쪽", "북서쪽"][Math.round(motion.headingDeg / 45) % 8],
        speedMps: motion.speedMps, displacementM: motion.displacementM, spanMs,
        sampleCount: trajectory.length, accuracyM, trajectory: trajectory.map(publicPoint),
      };
    }
    return { ...context, reason };
  }

  function predictStop(motion, candidates) {
    const empty = (reason, scores = []) => ({ key: null, reason, scores });
    if (motion?.status !== "moving" || !finite(motion.headingDeg) || !validPoint(motion.latest)
      || !Array.isArray(motion.trajectory) || motion.trajectory.length < 4) return empty("motion_uncertain");
    const accuracyM = finite(motion.accuracyM) ? motion.accuracyM : MAX_ACCURACY_M;
    const nearby = (Array.isArray(candidates) ? candidates : []).filter((candidate) =>
      typeof candidate?.key === "string" && validPoint(candidate) && finite(candidate.distanceM)
      && candidate.distanceM >= 0 && candidate.distanceM <= 30)
      .map((candidate) => ({ ...candidate, currentDistanceM: distance(motion.latest, candidate) }))
      .filter((candidate) => candidate.currentDistanceM <= 30);
    if (!nearby.length) return empty("no_nearby_stop");
    if (nearby.some((candidate) => candidate.currentDistanceM <= Math.max(4, Math.min(10, accuracyM)))) {
      return empty("at_stop");
    }

    const scores = [];
    const seen = new Set();
    for (const candidate of nearby) {
      if (seen.has(candidate.key)) continue;
      seen.add(candidate.key);
      const delta = offset(motion.latest, candidate);
      const angleDeg = angleDifference(motion.headingDeg, heading(delta.x, delta.y));
      if (angleDeg > 55) continue;
      const distances = motion.trajectory.map((point) => ({ ...point, distanceM: distance(point, candidate) }));
      const approachM = distances[0].distanceM - distances.at(-1).distanceM;
      const velocities = slopes(distances, (point) => point.distanceM, 3);
      const approachSpeedMps = -median(velocities);
      const decreasingShare = velocities.filter((speed) => speed < -0.15).length / Math.max(1, velocities.length);
      if (approachM < Math.max(3, accuracyM * 0.7) || approachSpeedMps < 0.25 || decreasingShare < 0.65) continue;
      const alignment = Math.cos(angleDeg * RAD);
      const approachRatio = Math.min(1, approachSpeedMps / Math.max(0.35, motion.speedMps));
      scores.push({ key: candidate.key, score: alignment * 0.65 + approachRatio * 0.35,
        angleDeg, distanceM: candidate.currentDistanceM, approachM, approachSpeedMps });
    }
    scores.sort((a, b) => b.score - a.score);
    if (!scores.length) return empty("no_approaching_stop");
    if (scores[1] && (scores[0].score - scores[1].score < 0.15
      || Math.abs(scores[0].angleDeg - scores[1].angleDeg) < 12)) return empty("ambiguous_stops", scores);
    // 반환값은 접근 궤적에 맞는 후보일 뿐, 사용자의 탑승 의도를 확정하지 않는다.
    return { key: scores[0].key, reason: "approaching_stop", scores };
  }

  window.GGpsMotion = { estimate, predictStop };
})();
