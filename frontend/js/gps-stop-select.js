/**
 * GPS·버스 도착 조회 결과로 안내할 정류장과 차량별 안내 단계를 판단한다.
 * 화면·지도·음성 재생과 분리해 기록 재생 테스트와 다른 앱 이식에 그대로 사용한다.
 * 상태는 호출자가 넘기는 추적 객체(tracker)에 저장하며, 현재 시각은 인자로 받는다.
 */
(() => {
  "use strict";

  const CONFIG = Object.freeze({
    maxAgeMs: 10000,
    stableCount: 3,
    switchHoldMs: 6000,
    switchSampleGapMs: 5000,
    liveMaxAccuracyM: 30,
    searchRadiusM: 30,
    refreshIntervalMs: 20000,
    // 보고 오차가 이보다 크면 실제 정류장이 조회 후보(측정 위치 30m)에서 빠질 수 있다.
    ambiguousAccuracyM: 15,
    maxEvents: 1000,
    // 버스 번호 인식은 이 시간 안의 촬영 프레임만 현재 근거로 본다.
    busDetectionWindowMs: 3000,
    // 프레임마다 들어오는 인식 결과는 같은 번호를 이 간격으로만 이력에 남긴다.
    busDetectionLogIntervalMs: 1000,
  });

  function distanceM(first, second) {
    const radius = 6371000;
    const toRadians = (value) => value * Math.PI / 180;
    const lat1 = toRadians(first.latitude);
    const lat2 = toRadians(second.latitude);
    const deltaLat = toRadians(second.latitude - first.latitude);
    const deltaLon = toRadians(second.longitude - first.longitude);
    const value = Math.sin(deltaLat / 2) ** 2
      + Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
    return radius * 2 * Math.atan2(Math.sqrt(value), Math.sqrt(1 - value));
  }

  function hasFreshLocation(position, now = Date.now()) {
    const timestamp = position?.timestamp;
    return Number.isFinite(timestamp) && timestamp <= now && now - timestamp <= CONFIG.maxAgeMs;
  }

  const hasCoordinates = (station) => Number.isFinite(station?.latitude) && Number.isFinite(station?.longitude);

  function nearbyMatches(result) {
    return result ? (Array.isArray(result.matches) ? result.matches : [result]) : [];
  }

  function arrivalCandidateKey(match) {
    const station = match?.station || {};
    const arrival = match?.arrival || {};
    return JSON.stringify([
      station.station_id || station.ars_id || station.station_name || "",
      match?.bus_route_id || arrival.route_id || "",
      Number.isInteger(arrival.station_order) ? arrival.station_order : arrival.direction || "",
    ]);
  }

  // 서버가 단계를 주지 않은 기존 응답은 도착 문구로 단계를 판단한다.
  function arrivalState(arrival) {
    const message = arrival?.first_arrival || "";
    return arrival?.first_arrival_state || (!message ? "unavailable"
      : /진입중|곧\s*도착/.test(message) ? "arriving" : "approaching");
  }

  function canAnnounceArrival(match) {
    return ["approaching", "arriving", "arrived"].includes(arrivalState(match?.arrival));
  }

  function arrivalCandidates(result) {
    const seen = new Set();
    return nearbyMatches(result)
      .filter(canAnnounceArrival)
      .filter((match) => {
        const key = arrivalCandidateKey(match);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort((left, right) => Number(left.station?.distance_m) - Number(right.station?.distance_m));
  }

  // 최근 GPS 궤적으로 접근 정류장을 추정하고 서로 다른 측정값의 연속 확인 여부를 갱신한다.
  function updateWalking(tracker, now = Date.now()) {
    const motionModule = window.GGpsMotion;
    const motion = motionModule.estimate(tracker.trackSamples, now);
    tracker.walkingMotion = motion;
    const current = motion.latest;
    const resultAge = now - tracker.lastNearbyResultAt;
    const matches = current && resultAge >= 0 && resultAge <= CONFIG.refreshIntervalMs
      ? arrivalCandidates(tracker.lastNearbyResult).filter((match) =>
        hasCoordinates(match.station) && distanceM(current, match.station) <= CONFIG.searchRadiusM)
      : [];
    const prediction = motionModule.predictStop(motion, matches.map((match) => ({
      key: arrivalCandidateKey(match), latitude: match.station.latitude,
      longitude: match.station.longitude, distanceM: distanceM(current, match.station),
    })));
    tracker.walkingPrediction = prediction;
    tracker.walkingMatch = matches.find((match) => arrivalCandidateKey(match) === prediction.key) || null;
    // 동일 좌표를 API 응답 때 다시 평가해도 확인 횟수가 늘어나지 않는다.
    const timestamp = motion.timestampMs;
    const awaitingResult = tracker.lastNearbyResult && resultAge > CONFIG.refreshIntervalMs
      && motion.status === "moving";
    // 재조회 응답 대기만으로 최근 GPS의 확인 이력을 지우지 않는다.
    if (!prediction.key && !awaitingResult) tracker.walkingVotes = [];
    tracker.walkingVotes = tracker.walkingVotes.filter((vote) => now - vote.timestamp <= CONFIG.maxAgeMs
      && vote.timestamp !== timestamp);
    if (!awaitingResult && Number.isFinite(timestamp) && now - timestamp <= CONFIG.maxAgeMs) {
      tracker.walkingVotes.push({ timestamp, key: prediction.key });
    }
    tracker.walkingVotes = tracker.walkingVotes.slice(-CONFIG.stableCount);
    tracker.walkingConfirmed = Boolean(prediction.key
      && tracker.walkingVotes.length === CONFIG.stableCount
      && tracker.walkingVotes.every((vote) => vote.key === prediction.key));
    const latestSample = tracker.trackSamples.at(-1);
    if (latestSample?.position.timestamp === timestamp) latestSample.walking = {
      status: motion.status, heading_deg: motion.headingDeg, speed_mps: motion.speedMps,
      destination_key: prediction.key, confirmed: tracker.walkingConfirmed,
    };
    return motion;
  }

  function stationSwitchDominates(tracker, latest, old, next, predicted) {
    if (!hasCoordinates(old?.station)) return true;
    const oldDistance = distanceM(latest, old.station);
    const nextDistance = distanceM(latest, next.station);
    if (oldDistance > CONFIG.searchRadiusM) return true;
    const margin = Math.max(3, Math.min(8, latest.accuracy * 0.6));
    const motion = tracker.walkingMotion;
    if (motion?.status !== "moving" || motion.trajectory?.length < 4) {
      return nextDistance + margin <= oldDistance;
    }
    const trajectory = motion.trajectory;
    const spanSec = (trajectory.at(-1).timestampMs - trajectory[0].timestampMs) / 1000;
    if (spanSec <= 0) return false;
    const approach = (station) => (distanceM(trajectory[0], station)
      - distanceM(trajectory.at(-1), station)) / spanSec;
    const oldApproach = approach(old.station);
    const nextApproach = approach(next.station);
    // 양쪽 모두 접근 중일 때는 더 먼 후보로 바꾸지 않는다.
    if (nextDistance + margin <= oldDistance) return nextApproach >= oldApproach - 0.2;
    return predicted && nextApproach >= 0.25 && oldApproach <= 0.1
      && nextApproach - oldApproach >= 0.35;
  }

  /**
   * 조회 결과에서 안내할 정류장 후보를 고른다. 기존 대상이 없으면 바로 고르고,
   * 다른 후보로 바꿀 때는 우세 근거가 일정 시간·횟수 유지돼야 한다.
   * speaking이 참이면 읽고 있는 문장이 끝날 때까지 전환을 미룬다.
   */
  function selectCandidate(tracker, result, { now = Date.now(), speaking = false } = {}) {
    const chosen = chooseCandidate(tracker, result, now, speaking);
    // 전환 확인 중에는 기존 안내 대상을 기준으로 판단한다.
    markAmbiguity(tracker, arrivalCandidates(result), chosen || tracker.selectedStationMatch, now);
    return chosen;
  }

  /**
   * 안내 대상과 다른 후보의 거리 차이가 보고 오차보다 작거나, 보고 오차가 커서 조회 후보 자체를
   * 믿을 수 없으면 위치만으로는 구분할 수 없다고 표시한다.
   * 판정은 바꾸지 않으며, 기록과 이후 버스 번호 인식 등 다른 근거를 쓸지 정하는 데 사용한다.
   * 기록 재생 평가에서 오차를 키울수록 처음 고른 정류장이 틀리는 원인이 이 상황이었다.
   */
  function markAmbiguity(tracker, candidates, chosen, now) {
    const latest = tracker.samples.at(-1);
    tracker.selectionAmbiguous = false;
    tracker.ambiguousStationKeys = [];
    if (!chosen || !hasCoordinates(chosen.station) || !latest || !hasFreshLocation(latest.position, now)
      || !Number.isFinite(latest.accuracy)) return;
    const chosenDistance = distanceM(latest, chosen.station);
    tracker.ambiguousStationKeys = candidates
      .filter((match) => arrivalCandidateKey(match) !== arrivalCandidateKey(chosen) && hasCoordinates(match.station)
        && Math.abs(distanceM(latest, match.station) - chosenDistance) < latest.accuracy)
      .map(arrivalCandidateKey);
    tracker.selectionAmbiguous = tracker.ambiguousStationKeys.length > 0
      || latest.accuracy > CONFIG.ambiguousAccuracyM;
  }

  function chooseCandidate(tracker, result, now, speaking) {
    const candidates = arrivalCandidates(result);
    tracker.selectionBasis = "recent_distance";
    if (!candidates.length) { tracker.switchCandidate = null; return null; }
    const previous = candidates.find((match) => arrivalCandidateKey(match) === tracker.selectedStationKey);
    const predicted = candidates.find((match) => arrivalCandidateKey(match) === tracker.walkingPrediction?.key);
    const latest = tracker.samples.at(-1);
    const closest = latest && hasFreshLocation(latest.position, now)
      ? candidates.reduce((best, match) => distanceM(latest, match.station)
        < distanceM(latest, best.station) ? match : best) : candidates[0];
    const next = predicted || closest;
    if (!tracker.selectedStationKey || arrivalCandidateKey(next) === tracker.selectedStationKey) {
      tracker.switchCandidate = null;
      if (predicted) tracker.selectionBasis = "walking_approach";
      return next;
    }
    const old = previous || tracker.selectedStationMatch;
    const valid = latest && hasFreshLocation(latest.position, now)
      && Number.isFinite(latest.accuracy) && latest.accuracy <= CONFIG.liveMaxAccuracyM
      && hasCoordinates(next.station)
      && distanceM(latest, next.station) <= CONFIG.searchRadiusM
      && stationSwitchDominates(tracker, latest, old, next, Boolean(predicted));
    if (!valid) {
      tracker.switchCandidate = null;
      tracker.selectionBasis = "switch_evidence_missing";
      return previous || null;
    }
    const timestamp = latest.position.timestamp;
    const key = arrivalCandidateKey(next);
    let pending = tracker.switchCandidate;
    // GPS 수신이 오래 비어 있으면 그 사이에도 우세했다고 간주하지 않는다.
    if (!pending || pending.key !== key || timestamp - pending.lastMs > CONFIG.switchSampleGapMs) {
      pending = { key, firstMs: timestamp, lastMs: timestamp, count: 1 };
    } else if (timestamp > pending.lastMs) {
      pending.lastMs = timestamp;
      pending.count += 1;
    }
    tracker.switchCandidate = pending;
    tracker.selectionBasis = "switch_confirmation_pending";
    if (pending.count < CONFIG.stableCount
      || timestamp - pending.firstMs < CONFIG.switchHoldMs) return previous || null;
    if (speaking) return previous || null;
    tracker.switchCandidate = null;
    tracker.selectionBasis = predicted ? "walking_approach" : "recent_distance";
    return next;
  }

  function vehicleIdentity(value) {
    const identity = String(value ?? "").trim();
    return /^0*$/.test(identity) ? "" : identity;
  }

  /**
   * 정류장·노선·정차 순번별로 차량과 도착 단계를 추적한다. 반환한 항목마다
   * 호출자가 안내를 만들고, 재생을 마치면 vehicle.announcedPhase를 phase까지 올린다.
   * 조회 결과에서 잠시 사라져도 완료 이력은 stops가 유지되는 동안 남는다.
   */
  function updateAnnouncementStops(stops, matches, busNumber) {
    for (const stop of stops.values()) stop.visible = false;
    const entries = [];
    for (const match of matches) {
      const arrival = match.arrival;
      const station = match.station || {};
      const order = arrival?.station_order;
      const key = JSON.stringify([
        station.station_id || station.ars_id || station.station_name,
        match.bus_route_id || arrival?.route_id || busNumber,
        Number.isInteger(order) && order > 0 ? order : arrival?.direction || "",
      ]);
      let stop = stops.get(key);
      if (!stop) {
        stop = { vehicles: [], current: null, phase: 0, visible: true };
        stops.set(key, stop);
      }
      stop.visible = true;
      const status = arrivalState(arrival);
      stop.phase = status === "unavailable" ? 0 : ["arriving", "arrived"].includes(status) ? 2 : 1;
      if (!stop.phase) continue;
      const id = vehicleIdentity(arrival?.first_vehicle_id);
      const plate = vehicleIdentity(arrival?.vehicle_number);
      let vehicle = id || plate
        ? stop.vehicles.find((item) => id && item.id ? item.id === id : plate && item.plate === plate)
        : stop.current;
      // 차량 ID가 누락됐다 복구되는 것은 다음 버스로 바뀐 것으로 보지 않는다.
      if (!vehicle && stop.current && !stop.current.id && !stop.current.plate) vehicle = stop.current;
      if (!vehicle) {
        vehicle = { id: "", plate: "", announcedPhase: 0, sequence: stop.vehicles.length };
        stop.vehicles.push(vehicle);
      }
      if (id) vehicle.id = id;
      if (plate) vehicle.plate = plate;
      stop.current = vehicle;
      entries.push({ key, stop, vehicle, phase: stop.phase, match });
    }
    return entries;
  }

  /**
   * 안내 판단과 음성 재생 이력을 시간순으로 남긴다. measurement.json은 최종 결과를 덮어쓰므로
   * 실제 안내 흐름(대상 전환, 재생 시작·완료, 조회 결과)은 이 목록으로 사후 검증한다.
   */
  function logEvent(tracker, type, fields = {}, now = Date.now()) {
    if (!tracker) return;
    tracker.events = tracker.events || [];
    tracker.events.push({ type, at_ms: now, ...fields });
    if (tracker.events.length > CONFIG.maxEvents) {
      tracker.eventsDropped = (tracker.eventsDropped || 0) + tracker.events.length - CONFIG.maxEvents;
      tracker.events = tracker.events.slice(-CONFIG.maxEvents);
    }
  }

  // 안내 대상을 확정하고 바뀐 경우에만 전환 근거와 함께 기록한다.
  function commitSelection(tracker, match, now = Date.now()) {
    const key = arrivalCandidateKey(match);
    if (key !== tracker.selectedStationKey) {
      logEvent(tracker, "selection", {
        from: tracker.selectedStationKey || null, to: key,
        station_name: match.station?.station_name || null, direction: match.arrival?.direction || null,
        basis: tracker.selectionBasis || null, ambiguous: Boolean(tracker.selectionAmbiguous),
      }, now);
    }
    tracker.selectedStationKey = key;
    tracker.selectedStationMatch = match;
  }

  const normalizeRouteNumber = (value) => String(value ?? "").replace(/\s+/g, "").toUpperCase();

  /**
   * 카메라의 버스 LED 번호 인식 결과를 받는 입력 지점이다. 시각은 프레임 촬영 시각(ms)을 사용해
   * GPS 측정 시각과 같은 기준으로 비교한다. 인식 결과는 기록만 하며 정류장 선택은 바꾸지 않는다.
   */
  function observeBusDetection(tracker, { routeNumber, capturedAtMs, confidence = null, trackId = null }) {
    const route = normalizeRouteNumber(routeNumber);
    if (!route || !Number.isFinite(capturedAtMs)) return false;
    const detection = { route_number: route, captured_at_ms: capturedAtMs, confidence, track_id: trackId };
    const recent = (tracker.busDetections || [])
      .filter((item) => capturedAtMs - item.captured_at_ms <= CONFIG.busDetectionWindowMs);
    const lastLogged = tracker.busDetectionLoggedAt?.[route];
    tracker.busDetections = [...recent, detection];
    if (!Number.isFinite(lastLogged) || capturedAtMs - lastLogged >= CONFIG.busDetectionLogIntervalMs) {
      tracker.busDetectionLoggedAt = { ...tracker.busDetectionLoggedAt, [route]: capturedAtMs };
      logEvent(tracker, "bus_detection", detection, capturedAtMs);
    }
    return true;
  }

  // 입력한 노선 번호와 같은 최근 인식 결과 중 가장 늦은 것을 돌려준다.
  function recentBusDetection(tracker, busNumber, now = Date.now()) {
    const route = normalizeRouteNumber(busNumber);
    return (tracker.busDetections || []).filter((item) => item.route_number === route
      && item.captured_at_ms <= now && now - item.captured_at_ms <= CONFIG.busDetectionWindowMs)
      .at(-1) || null;
  }

  function walkingRecord(tracker) {
    const motion = tracker?.walkingMotion;
    if (!motion) return { walking_motion: null, walking_destination: null };
    const { trajectory, ...summary } = motion;
    return {
      walking_motion: summary,
      walking_destination: tracker.walkingPrediction?.key ? {
        key: tracker.walkingPrediction.key,
        station: tracker.walkingMatch?.station || null,
        direction: tracker.walkingMatch?.arrival?.direction || null,
        confirmed: tracker.walkingConfirmed,
        reason: tracker.walkingPrediction.reason,
      } : null,
      selected_station_key: tracker.selectedStationKey || null,
      station_selection_basis: tracker.selectionBasis || null,
      station_selection_ambiguous: Boolean(tracker.selectionAmbiguous),
    };
  }

  // GPS 기록 파일에 저장하는 판단 결과와 이벤트 이력
  function trackingRecord(tracker) {
    return { ...walkingRecord(tracker), events: tracker?.events || [], events_dropped: tracker?.eventsDropped || 0 };
  }

  window.GStopSelect = {
    CONFIG, distanceM, hasFreshLocation, nearbyMatches, arrivalCandidateKey, arrivalState,
    canAnnounceArrival, arrivalCandidates, updateWalking, selectCandidate,
    updateAnnouncementStops, walkingRecord, trackingRecord, logEvent, commitSelection,
    observeBusDetection, recentBusDetection,
  };
})();
