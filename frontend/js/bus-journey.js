/** GPS 정류장 도착정보와 카메라의 노선 번호 인식을 각각 추적한다. */
(() => {
  "use strict";
  const normalize = value => String(value ?? "").replace(/\s+/g, "").toUpperCase();
  const ROUTE = /^[0-9A-Z가-힣-]{1,20}$/;

  function create({ api, coordinator, onChange = () => {}, onStatus = () => {},
    onEvent = () => {}, now = () => Date.now(), monotonicNow = () => performance.now(),
    geolocation = navigator.geolocation } = {}) {
    const select = window.GStopSelect;
    const limits = select.CONFIG;
    let active = false, paused = false, route = "", generation = 0;
    let watchId = null, ticker = null, request = null, tracker = null, location = null;
    let positionPoll = null, lastPositionPollAt = -Infinity, locationDenied = false;
    let arrivalUnavailable = false;
    let lastLookupAt = -Infinity, lastLookupPosition = null, lastOcrAt = -Infinity;
    let observationStartedAt = 0;
    let manualStationKey = null, gps = {}, ocr = {}, arrivalEntry = null;
    let ocrPending = null, speakingArrival = null, speakingOcr = null;
    const ocrAnnounced = new Set();
    const otherRouteAnnouncedAt = new Map();
    const emit = (type, fields = {}) => onEvent({ type, at_ms: now(), route, ...fields });
    const snapshot = () => ({ active, paused, route, gps: { ...gps }, ocr: { ...ocr } });
    const publish = () => onChange(snapshot());
    const freshCapture = capturedAt => Number.isFinite(capturedAt) && capturedAt <= now()
      && now() - capturedAt <= limits.busDetectionWindowMs;
    const freshLocation = () => location && select.hasFreshLocation(location.position, now())
      && Number.isFinite(location.accuracy) && location.accuracy >= 0
      && location.accuracy <= limits.liveMaxAccuracyM;
    const validGeneration = token => active && !paused && generation === token;
    const otherRecentlyAnnounced = value => now() - (otherRouteAnnouncedAt.get(value) ?? -Infinity) < 10000;

    function newTracker() {
      return { busNumber: route, samples: [], trackSamples: [], walkingVotes: [],
        selectedStationKey: "", selectedStationMatch: null, switchCandidate: null,
        lastNearbyResult: null, lastNearbyResultAt: 0, announcementStops: new Map() };
    }

    function clearSpeech(source) {
      coordinator.clear(source);
      if (source === "bus-arrival") speakingArrival = null;
      if (source === "bus-ocr") speakingOcr = null;
    }

    function gpsUnavailable(status, message, details = {}) {
      gps = { ...gps, status, message, selected: null, candidates: [], ...details };
      arrivalEntry = null;
      clearSpeech("bus-arrival");
      onStatus(message);
      publish();
    }

    function clearResources() {
      generation++;
      if (watchId !== null) geolocation?.clearWatch?.(watchId);
      watchId = null;
      clearInterval(ticker);
      ticker = null;
      request?.abort();
      request = null;
      positionPoll = null;
      clearSpeech("bus-arrival");
      clearSpeech("bus-ocr");
    }

    function resetFreshness({ preserveSelection = false } = {}) {
      const previous = tracker;
      observationStartedAt = now();
      positionPoll = null;
      lastPositionPollAt = -Infinity;
      locationDenied = false;
      arrivalUnavailable = false;
      tracker = newTracker();
      if (preserveSelection && previous) {
        tracker.announcementStops = previous.announcementStops;
        if (manualStationKey) {
          tracker.selectedStationKey = manualStationKey;
          tracker.selectedStationMatch = previous.selectedStationMatch;
        }
      }
      location = null;
      lastLookupPosition = null;
      lastLookupAt = -Infinity;
      lastOcrAt = -Infinity;
      if (!preserveSelection) manualStationKey = null;
      arrivalEntry = null;
      ocrPending = null;
      gps = { status: "locating", message: "현재 위치와 주변 정류장을 확인하고 있습니다.",
        selected: null, candidates: [], lastUpdatedAt: null };
      ocr = { status: "searching", message: `${route}번 버스 번호를 카메라로 확인합니다.`,
        routeNumber: null, confirmed: false, capturedAt: null };
    }

    function candidateView(match) {
      return { ...match, key: select.arrivalCandidateKey(match) };
    }

    function arrivalIsFresh(entry) {
      return active && !paused && freshLocation() && entry === arrivalEntry
        && now() - tracker.lastNearbyResultAt <= limits.refreshIntervalMs
        && entry.stop.current === entry.vehicle && entry.stop.phase === entry.phase;
    }

    function sayArrival(replay = false) {
      const entry = arrivalEntry;
      if (!entry || !arrivalIsFresh(entry) || speakingArrival
          || (!replay && entry.vehicle.announcedPhase >= entry.phase)) return false;
      const text = entry.match.announcement;
      if (!text) return false;
      const token = generation;
      const expires = Math.min(location.position.timestamp + limits.maxAgeMs,
        tracker.lastNearbyResultAt + limits.refreshIntervalMs);
      speakingArrival = entry;
      const accepted = coordinator.request({ source: "bus-arrival", priority: coordinator.PRIORITY.busArrival,
        dynamic: true, text, kind: entry.key,
        validUntil: monotonicNow() + Math.max(0, expires - now()),
        metadata: { bus_route: route, station_key: entry.key, channel: "gps" },
        onStart: () => emit("bus_arrival_speech_started", { text }),
        onComplete: () => {
          if (!validGeneration(token)) return;
          entry.vehicle.announcedPhase = Math.max(entry.vehicle.announcedPhase, entry.phase);
          if (speakingArrival === entry) speakingArrival = null;
          emit("bus_arrival_speech_completed", { text });
        },
        onCancel: () => { if (speakingArrival === entry) speakingArrival = null; },
      });
      if (!accepted && speakingArrival === entry) speakingArrival = null;
      return accepted;
    }

    function sayOcr(replay = false) {
      const item = ocrPending;
      if (!item || !active || paused || !freshCapture(item.capturedAt) || speakingOcr
          || (!replay && ocrAnnounced.has(item.key))) return false;
      const alreadyConfirmed = item.state === "recognized_single" && ocrAnnounced.has(
        `${item.track_id ?? "unknown"}:${item.route_number}:confirmed`);
      if (!replay && (alreadyConfirmed || !item.isTarget && otherRecentlyAnnounced(item.route_number))) return false;
      const token = generation;
      // Keep target repetitions in one cancellable playback; other routes need one brief cue.
      const speechText = item.isTarget ? `${item.message} ${item.message}`
        : "다른 버스. 목표 버스를 계속 찾는 중.";
      speakingOcr = item;
      item.started = false;
      const accepted = coordinator.request({ source: "bus-ocr", priority: coordinator.PRIORITY.busOcr,
        dynamic: true, text: speechText, kind: item.key,
        validUntil: monotonicNow() + Math.max(0, item.capturedAt + limits.busDetectionWindowMs - now()),
        metadata: { bus_route: item.route_number, target_route: route, is_target: item.isTarget,
          captured_at_ms: item.capturedAt, track_id: item.track_id, channel: "ocr" },
        onStart: () => {
          if (!validGeneration(token) || speakingOcr !== item) return;
          item.started = true;
          emit("bus_ocr_speech_started", {
            text: speechText, captured_at_ms: item.capturedAt, track_id: item.track_id ?? null,
            voice_delay_ms: Math.max(0, now() - item.capturedAt),
          });
        },
        onComplete: () => {
          if (!validGeneration(token) || speakingOcr !== item) return;
          ocrAnnounced.add(item.key);
          if (ocrAnnounced.size > 200) ocrAnnounced.delete(ocrAnnounced.values().next().value);
          if (!item.isTarget) otherRouteAnnouncedAt.set(item.route_number, now());
          if (otherRouteAnnouncedAt.size > 200) otherRouteAnnouncedAt.delete(otherRouteAnnouncedAt.keys().next().value);
          if (speakingOcr === item) speakingOcr = null;
          emit("bus_ocr_speech_completed", { text: speechText });
        },
        onCancel: () => { if (speakingOcr === item) speakingOcr = null; },
      });
      if (!accepted && speakingOcr === item) speakingOcr = null;
      return accepted;
    }

    function applySelection() {
      if (!freshLocation() || !tracker.lastNearbyResult
          || now() - tracker.lastNearbyResultAt > limits.refreshIntervalMs) return;
      // 선택은 최신 GPS 위치를 기준으로 평가하고 30m를 벗어난 정류장은 제외한다.
      const matches = select.nearbyMatches(tracker.lastNearbyResult)
        .filter(item => Number.isFinite(item.station?.latitude) && Number.isFinite(item.station?.longitude))
        .map(item => ({ ...item, station: { ...item.station,
          distance_m: select.distanceM(location, item.station) } }))
        .filter(item => item.station.distance_m <= limits.searchRadiusM);
      const result = { ...tracker.lastNearbyResult, matches };
      select.updateWalking(tracker, now());
      const candidates = select.arrivalCandidates(result);
      let options = [...new Map(matches.map(item => [select.arrivalCandidateKey(item), item])).values()];
      let selected = manualStationKey
        ? matches.find(item => select.arrivalCandidateKey(item) === manualStationKey) : null;
      if (manualStationKey && !selected) {
        const previous = tracker.selectedStationMatch;
        // 도착정보 없이 선택한 정류장에 단일 방향 정보가 새로 생긴 경우 키를 보완한다.
        if (previous && !Number.isInteger(previous.arrival?.station_order)) {
          const identified = matches.filter(item => item.station.station_id === previous.station.station_id
            && item.bus_route_id === previous.bus_route_id);
          if (identified.length === 1) {
            selected = identified[0];
            manualStationKey = select.arrivalCandidateKey(selected);
          }
        }
        if (!selected && previous?.station && select.distanceM(location, previous.station) <= limits.searchRadiusM) {
          // 선택한 방향의 도착정보가 잠시 빠져도 다른 방향으로 바꾸지 않는다.
          selected = { ...previous, station: { ...previous.station,
            distance_m: select.distanceM(location, previous.station) },
            arrival: { ...previous.arrival, first_arrival: "", first_arrival_state: "unavailable" },
            announcement: "선택한 정류장의 도착정보를 기다리고 있습니다." };
          options = options.filter(item => !(item.arrival == null
            && item.station.station_id === previous.station.station_id
            && item.bus_route_id === previous.bus_route_id));
          options.push(selected);
        } else if (!selected) manualStationKey = null;
      }
      if (!selected) selected = select.selectCandidate(tracker, result,
        { now: now(), speaking: Boolean(speakingArrival) });
      if (!selected) {
        gpsUnavailable("searching", candidates.length
          ? "정류장 위치를 다시 확인하고 있습니다."
          : `주변 ${route}번 정류장의 도착정보를 기다리고 있습니다.`,
          { candidates: options.map(candidateView), lastUpdatedAt: tracker.lastNearbyResultAt });
        return;
      }
      const previousKey = tracker.selectedStationKey;
      select.commitSelection(tracker, selected, now());
      if (previousKey !== tracker.selectedStationKey) {
        clearSpeech("bus-arrival");
        emit("bus_station_selected", { key: tracker.selectedStationKey,
          basis: manualStationKey ? "user" : tracker.selectionBasis });
      }
      const available = select.canAnnounceArrival(selected);
      const previousStatus = gps.status;
      gps = { status: available ? "ready" : "waiting", message: selected.announcement || selected.arrival?.first_arrival
          || "선택한 정류장의 도착정보를 기다리고 있습니다.",
        selected: candidateView(selected), candidates: options.map(candidateView),
        ambiguous: !manualStationKey && Boolean(tracker.selectionAmbiguous),
        lastUpdatedAt: tracker.lastNearbyResultAt, accuracyM: location.accuracy };
      arrivalEntry = select.updateAnnouncementStops(tracker.announcementStops, [selected], route)[0] || null;
      if (speakingArrival && (speakingArrival.key !== arrivalEntry?.key
          || speakingArrival.vehicle !== arrivalEntry?.vehicle || speakingArrival.phase !== arrivalEntry?.phase)) {
        clearSpeech("bus-arrival");
      }
      publish();
      if (available && previousStatus !== "ready") onStatus("현재 위치와 정류장을 확인했습니다. 버스 도착정보를 안내합니다.");
      sayArrival();
    }

    async function lookup() {
      if (!active || paused || arrivalUnavailable || request || !freshLocation()) return;
      const token = generation;
      const sampled = location;
      const controller = new AbortController();
      request = controller;
      lastLookupAt = now();
      lastLookupPosition = { latitude: sampled.latitude, longitude: sampled.longitude };
      try {
        const result = await api.nearbyBusArrival({ busNumber: route, latitude: sampled.latitude,
          longitude: sampled.longitude, accuracyM: sampled.accuracy, signal: controller.signal });
        if (!validGeneration(token) || controller.signal.aborted) return;
        // 응답이 오기 전에 위치가 오래되거나 조회 위치에서 멀어진 결과는 안내에 쓰지 않는다.
        if (!select.hasFreshLocation(sampled.position, now()) || !freshLocation()
            || select.distanceM(sampled, location) >= 14) {
          lastLookupAt = -Infinity;
          return;
        }
        tracker.lastNearbyResult = result;
        tracker.lastNearbyResultAt = now();
        emit("bus_arrival_result", { matches: select.nearbyMatches(result) });
        applySelection();
      } catch (error) {
        if (!validGeneration(token) || controller.signal.aborted || error?.name === "AbortError") return;
        const searching = ["nearby_station_not_found", "bus_route_not_nearby"].includes(error?.code);
        const unconfigured = error?.code === "bus_api_unconfigured";
        tracker.lastNearbyResult = null;
        if (unconfigured) {
          // Only arrival lookup needs the API key. Keep the OCR timer and speech alive.
          arrivalUnavailable = true;
          if (watchId !== null) geolocation?.clearWatch?.(watchId);
          watchId = null;
          positionPoll = null;
          location = null;
        }
        gpsUnavailable(searching ? "searching" : unconfigured ? "unavailable" : "error", searching
          ? `현재 위치 30m 안에서 ${route}번 정류장을 찾고 있습니다.`
          : `${{
            bus_api_unconfigured: "서버에 버스 API 인증키가 설정되지 않았습니다.",
            bus_api_auth_failed: "버스 API 인증에 실패해 도착정보를 사용할 수 없습니다.",
            bus_api_unreachable: "서울시 버스 API에 연결하지 못해 도착정보를 사용할 수 없습니다.",
            bus_api_error: "서울시 버스 API가 오류를 반환해 도착정보를 사용할 수 없습니다.",
            bus_api_http_error: "서울시 버스 API 요청에 실패해 도착정보를 사용할 수 없습니다.",
            bus_api_invalid_response: "서울시 버스 API 응답을 읽지 못해 도착정보를 사용할 수 없습니다.",
          }[error?.code] || "도착정보를 사용할 수 없습니다."} 카메라 번호 인식은 계속됩니다.`);
        emit("bus_arrival_error", { code: error?.code || "lookup_failed" });
      } finally {
        if (request === controller) request = null;
      }
    }

    function positionReceived(position, token) {
      if (!validGeneration(token) || arrivalUnavailable) return;
      const coords = position?.coords;
      if (location && Number.isFinite(position?.timestamp) && position.timestamp < location.position.timestamp) return;
      if (!coords || !Number.isFinite(coords.latitude) || Math.abs(coords.latitude) > 90
          || !Number.isFinite(coords.longitude) || Math.abs(coords.longitude) > 180
          || !select.hasFreshLocation(position, now()) || position.timestamp < observationStartedAt) {
        location = null;
        gpsUnavailable("stale", "최신 GPS 위치를 기다리고 있습니다. 버스 번호 인식은 계속됩니다.");
        return;
      }
      location = { latitude: coords.latitude, longitude: coords.longitude, accuracy: coords.accuracy, position };
      if (!freshLocation()) {
        gpsUnavailable("inaccurate", "GPS 오차가 커서 위치를 다시 확인합니다. 버스 번호 인식은 계속됩니다.");
        return;
      }
      tracker.samples = [...tracker.samples.filter(item => item.position.timestamp !== position.timestamp), location].slice(-32);
      tracker.trackSamples = [...tracker.trackSamples.filter(item => item.position.timestamp !== position.timestamp), location]
        .filter(item => now() - item.position.timestamp <= 30000).slice(-128);
      const moved = lastLookupPosition ? select.distanceM(lastLookupPosition, location) : Infinity;
      if (!tracker.lastNearbyResult || moved >= 14 || now() - lastLookupAt >= limits.refreshIntervalMs) {
        // 문제 응답도 정해진 갱신 주기를 지켜 API 키 오류 등의 반복 요청을 막는다.
        if (moved >= 14 || now() - lastLookupAt >= limits.refreshIntervalMs) void lookup();
      } else applySelection();
    }

    function locationFailed(error, token) {
      if (!validGeneration(token) || arrivalUnavailable) return;
      // 보조 조회의 일시적인 실패가 방금 받은 정상 watch 위치를 지우지 않게 한다.
      if (error?.code !== 1 && freshLocation()) {
        emit("bus_location_error", { code: error?.code || null });
        return;
      }
      if (error?.code === 1) { locationDenied = true; positionPoll = null; }
      location = null;
      request?.abort();
      request = null;
      gpsUnavailable(error?.code === 1 ? "denied" : "error", error?.code === 1
        ? "위치 권한이 없어 GPS 도착정보를 사용할 수 없습니다. 버스 번호 인식은 계속됩니다."
        : "GPS 위치를 받지 못했습니다. 버스 번호 인식은 계속됩니다.");
      emit("bus_location_error", { code: error?.code || null });
      if (error?.code === 1 && watchId !== null) {
        geolocation?.clearWatch?.(watchId);
        watchId = null;
      }
    }

    function pollPosition() {
      if (!active || paused || arrivalUnavailable || locationDenied || positionPoll
          || typeof geolocation?.getCurrentPosition !== "function"
          || now() - lastPositionPollAt < 5000) return;
      const token = generation;
      const pending = { at: now() };
      positionPoll = pending;
      lastPositionPollAt = pending.at;
      const complete = callback => value => {
        if (!validGeneration(token) || positionPoll !== pending) return;
        positionPoll = null;
        callback(value, token);
      };
      try {
        // 정류장에 서 있으면 watch 콜백이 멈출 수 있으므로 최신 위치를 주기적으로 요청한다.
        geolocation.getCurrentPosition(complete(positionReceived), complete(locationFailed),
          { enableHighAccuracy: true, maximumAge: 0, timeout: 4000 });
      } catch (error) { complete(locationFailed)(error); }
    }

    function tick() {
      if (!active || paused) return;
      // 브라우저가 timeout 콜백을 누락하더라도 이전 콜백은 무효화하고 조회를 복구한다.
      if (positionPoll && now() - positionPoll.at > 5000) positionPoll = null;
      pollPosition();
      if (request && now() - lastLookupAt > limits.maxAgeMs) {
        request.abort();
        request = null;
        lastLookupAt = now();
        gpsUnavailable("error", "도착정보 응답이 늦어 다시 조회합니다. 카메라 번호 인식은 계속됩니다.");
        emit("bus_arrival_error", { code: "lookup_timeout" });
      }
      if (location && !freshLocation() && !["stale", "inaccurate"].includes(gps.status)) {
        gpsUnavailable("stale", "GPS 위치가 오래되어 새 위치를 기다립니다. 버스 번호 인식은 계속됩니다.");
      }
      if (["ready", "waiting"].includes(gps.status) && now() - tracker.lastNearbyResultAt > limits.refreshIntervalMs) {
        gpsUnavailable("refreshing", "버스 도착정보를 갱신하고 있습니다.");
      }
      if (freshLocation() && now() - lastLookupAt >= limits.refreshIntervalMs) void lookup();
      if (["candidate", "confirmed", "other"].includes(ocr.status) && !freshCapture(ocr.capturedAt)) {
        ocr = { ...ocr, status: "stale", confirmed: false,
          message: "번호 인식 결과가 오래되었습니다. 현재 버스 번호를 다시 확인합니다." };
        ocrPending = null;
        // 최신 프레임으로 시작한 단발 안내는 현재 문장을 마치게 한다.
        if (!speakingOcr?.started) clearSpeech("bus-ocr");
        publish();
      }
      sayOcr();
      sayArrival();
    }

    function begin() {
      const token = generation;
      publish();
      ticker = setInterval(tick, 1000);
      if (typeof geolocation?.watchPosition !== "function"
          && typeof geolocation?.getCurrentPosition !== "function") {
        gpsUnavailable("unavailable", "이 브라우저는 위치 조회를 지원하지 않습니다. 버스 번호 인식은 계속됩니다.");
        return;
      }
      try {
        if (typeof geolocation.watchPosition === "function") {
          watchId = geolocation.watchPosition(position => positionReceived(position, token),
            error => locationFailed(error, token), { enableHighAccuracy: true, maximumAge: 0, timeout: 10000 });
        }
      } catch (error) { locationFailed(error, token); }
      pollPosition();
    }

    function start(value) {
      const next = normalize(value);
      if (!ROUTE.test(next)) throw new Error("버스 노선 번호를 확인해 주세요.");
      clearResources();
      route = next;
      active = true;
      paused = false;
      ocrAnnounced.clear();
      otherRouteAnnouncedAt.clear();
      resetFreshness();
      emit("bus_journey_started");
      begin();
      return snapshot();
    }

    function recognitionMessage(result) {
      if (result.status === "loading" || result.status === "idle") return `${route}번 버스 번호 인식을 준비하고 있습니다.`;
      if (result.status === "unavailable") return "카메라 번호 인식에 필요한 모델 파일이 없습니다.";
      const code = typeof result.error === "string" ? result.error : result.error?.code || "";
      if (code.startsWith("bus_model_load_failed")) return "카메라 번호 인식 모델을 불러오지 못했습니다. 종료 후 다시 시작해 주세요.";
      return result.message || result.error?.message || "카메라 번호 인식 중 오류가 발생했습니다. 다음 영상을 다시 확인합니다.";
    }

    function recognitionState(status, message, capturedAt = null) {
      ocr = { status, message, confirmed: false, routeNumber: null, capturedAt };
      ocrPending = null;
      clearSpeech("bus-ocr");
      publish();
    }

    function accept(result, capturedAtMs = result?.captured_at_ms) {
      if (!active || paused || !result) return;
      const event = result.event || result;
      if (event.target_route && normalize(event.target_route) !== route) return;
      const preparing = ["loading", "idle"].includes(result.status);
      const failed = event.errors?.length || ["error", "unavailable", "disabled"].includes(result.status);
      const capturedAt = capturedAtMs == null ? NaN : Number(capturedAtMs);
      // Loading/missing-model failures can arrive before the worker has a source frame.
      if (!Number.isFinite(capturedAt)) {
        if (preparing || failed) recognitionState(preparing ? "loading" : "error", recognitionMessage(result));
        return;
      }
      if (capturedAt < observationStartedAt
          || capturedAt < lastOcrAt || capturedAt > now()) return;
      lastOcrAt = capturedAt;
      if (preparing) {
        recognitionState("loading", recognitionMessage(result), capturedAt);
        return;
      }
      if (!freshCapture(capturedAt)) {
        ocr = { status: "stale", message: "카메라 결과가 늦게 도착했습니다. 현재 번호를 다시 확인합니다.",
          routeNumber: null, confirmed: false, capturedAt };
        ocrPending = null;
        if (!speakingOcr?.started) clearSpeech("bus-ocr");
        publish();
        return;
      }
      const matches = (failed ? [] : Array.isArray(event.matches) ? event.matches : [])
        .filter(item => normalize(item.route_number) === route
          && ["recognized_single", "matched_candidate"].includes(item.state))
        .sort((a, b) => Number(b.state === "matched_candidate") - Number(a.state === "matched_candidate")
          || (Number(b.token_score) || 0) - (Number(a.token_score) || 0));
      // Raw boxes/text remain visual evidence. Only the backend's repeated,
      // full-number recognition may identify a different bus aloud.
      const others = (failed ? [] : Array.isArray(event.recognized_routes) ? event.recognized_routes : [])
        .filter(item => item.is_target === false && item.state === "matched_candidate"
          && item.track_id != null && ROUTE.test(normalize(item.route_number))
          && normalize(item.route_number) !== route)
        .sort((a, b) => (Number(b.token_score) || 0) - (Number(a.token_score) || 0));
      const shown = matches[0] || others.find(item => !otherRecentlyAnnounced(normalize(item.route_number))
        && !ocrAnnounced.has(`${item.track_id}:${normalize(item.route_number)}:confirmed`)) || others[0];
      if (!shown) {
        // 단일 프레임에서 번호가 안 읽혀도 3초 이내 근거의 안내 문장을 잘라 버리지 않는다.
        if (!failed && ["candidate", "confirmed", "other"].includes(ocr.status) && freshCapture(ocr.capturedAt)) return;
        ocr = { status: failed ? "error" : "searching", confirmed: false, routeNumber: null, capturedAt,
          message: failed ? recognitionMessage(result)
            : `${route}번 버스 번호를 찾고 있습니다.` };
        ocrPending = null;
        if (failed || !speakingOcr?.started) clearSpeech("bus-ocr");
        publish();
        return;
      }
      const confirmed = shown.state === "matched_candidate";
      const observedRoute = normalize(shown.route_number);
      const isTarget = observedRoute === route;
      const message = !isTarget ? `${observedRoute}번 버스, 다른 노선.`
        : confirmed ? `${route}번 버스 확인함.` : `${route}번 버스 인식 중.`;
      ocr = { status: !isTarget ? "other" : confirmed ? "confirmed" : "candidate",
        message, routeNumber: observedRoute, isTarget, confirmed, capturedAt,
        trackId: shown.track_id ?? null };
      select.observeBusDetection(tracker, { routeNumber: observedRoute, capturedAtMs: capturedAt,
        confidence: shown.token_score, trackId: shown.track_id });
      const key = `${shown.track_id ?? "unknown"}:${observedRoute}:${confirmed ? "confirmed" : "candidate"}`;
      // A confirmed target immediately replaces its tentative announcement or
      // another route's speech, while safety speech retains its priority.
      if (isTarget && speakingOcr && (!speakingOcr.isTarget
          || confirmed && speakingOcr.state === "recognized_single")) clearSpeech("bus-ocr");
      ocrPending = { ...shown, route_number: observedRoute, isTarget, key, message, capturedAt };
      publish();
      sayOcr();
    }

    function selectStop(key) {
      if (!active || paused || !gps.candidates?.some(item => item.key === key)) return false;
      manualStationKey = key;
      clearSpeech("bus-arrival");
      applySelection();
      return true;
    }

    function pause() {
      if (!active || paused) return;
      paused = true;
      clearResources();
      gps = { ...gps, status: "paused", message: "버스 도착정보 안내를 일시중지했습니다.", selected: null, candidates: [] };
      ocr = { ...ocr, status: "paused", confirmed: false, message: "버스 번호 인식을 일시중지했습니다." };
      publish();
    }

    function resume() {
      if (!active || !paused) return;
      paused = false;
      resetFreshness({ preserveSelection: true });
      begin();
    }

    function stop() {
      clearResources();
      active = false;
      paused = false;
      route = "";
      arrivalEntry = ocrPending = null;
      ocrAnnounced.clear();
      otherRouteAnnouncedAt.clear();
      gps = { status: "idle", message: "", selected: null, candidates: [] };
      ocr = { status: "idle", message: "", confirmed: false, routeNumber: null, capturedAt: null };
      publish();
    }

    function repeat() {
      if (!active || paused) return false;
      if (ocrPending && freshCapture(ocrPending.capturedAt)) {
        clearSpeech("bus-ocr");
        return sayOcr(true);
      }
      if (arrivalEntry && arrivalIsFresh(arrivalEntry)) {
        clearSpeech("bus-ocr");
        clearSpeech("bus-arrival");
        return sayArrival(true);
      }
      return false;
    }

    return { start, accept, pause, resume, stop, repeat, selectStop, snapshot };
  }
  window.GBusJourney = { create };
})();
