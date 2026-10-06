/** One camera session with independent GPS arrival and visual bus recognition. */
(async () => {
  "use strict";
  const $ = id => document.getElementById(id);
  let ready = false, settings, player, coordinator, walking, traffic, boarding, journey;
  let sessionId = null, running = false, starting = false, stopping = false, paused = false;
  let automaticPause = false, generation = 0, presentation = 0, cameraLost = false;
  let frameId = 0, timer, tick, request, frameTask;
  let boardingState = null, activeRoute = null, draftRoute = "", lastResult = null;
  let timingQueue = [], eventQueue = [], flushTask = null, logTimer;
  let rawRecording = false, overlayRecording = false;
  const preferenceKey = "gildongmu-accessibility-v1";
  let preferences = { rate: 1, textScale: 1 };
  try {
    const stored = JSON.parse(localStorage.getItem(preferenceKey) || "{}");
    if (Number.isFinite(stored.rate)) preferences.rate = Math.max(.75, Math.min(2, stored.rate));
    if ([1, 1.2, 2].includes(stored.textScale)) preferences.textScale = stored.textScale;
  } catch (_) { /* Storage may be disabled. */ }
  const view = GView.create({ onAction: action => act(action),
    onSubmitRoute: confirmDraft, onStationChange: key => journey?.selectStop(key),
    onRateChange: rate => { preferences.rate = player?.setRate(rate) ?? rate; savePreferences(); },
    onTextScaleChange: textScale => { preferences.textScale = textScale; savePreferences(); } });
  view.setSettings(preferences); view.show("home"); view.setBusy(true);

  function savePreferences() {
    try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch (_) {}
  }
  function status(message) { view.setStatus(message); }
  function controls() {
    view.setBusy(!ready || starting || stopping || Boolean(boardingState?.busy));
    for (const id of ["device", "custom-device", "note"]) $(id).disabled = running || starting;
    $("camera-badge").textContent = paused ? "일시중지" : running ? "안내 중" : starting ? "준비 중" : "대기 중";
    $("camera-badge").classList.toggle("live", running && !paused);
  }
  function queueTiming(record) {
    if (!Number.isInteger(record.frame_id) || !Number.isInteger(record.captured_at_ms)) return;
    if (timingQueue.length >= 500) timingQueue.shift();
    timingQueue.push(record);
    if (timingQueue.length >= 20) void flushLogs();
  }
  function queueEvent(event) {
    if (!sessionId) return;
    if (eventQueue.length >= 200) eventQueue.shift();
    eventQueue.push(event);
    if (eventQueue.length >= 10) void flushLogs();
  }
  function flushLogs(id = sessionId) {
    if (!id || flushTask) return flushTask || Promise.resolve();
    flushTask = (async () => {
      for (const [queue, upload, limit] of [[timingQueue, GApi.timings, 100], [eventQueue, GApi.busEvents, 25]]) {
        while (queue.length) {
          const batch = queue.splice(0, limit);
          try { await upload(id, batch); }
          catch (error) { queue.unshift(...batch); console.warn("기록 전송 실패:", error.message); break; }
        }
      }
    })().finally(() => { flushTask = null; });
    return flushTask;
  }
  function speak(text) {
    if (!ready || !text) return;
    player.recordingStream();
    if (!running) coordinator.start();
    coordinator.request({ source: "interface", priority: coordinator.PRIORITY.boarding, text,
      dynamic: true, validUntil: performance.now() + 10000 });
  }
  const normalizeRoute = value => String(value || "").trim().replace(/\s+/g, "").replace(/번$/, "").toUpperCase();
  function confirmDraft(value) {
    const route = normalizeRoute(value);
    if (!/^(?:[가-힣]+|[A-Z]+)?[0-9]+[A-Z]?(?:-[0-9]+)?$/.test(route) || route.length > 20) {
      status("143, N26, 마포07처럼 노선 번호를 입력해 주세요.");
      view.announce("버스 번호 형식을 확인해 주세요."); return;
    }
    draftRoute = route; view.setRoute(route); view.show("confirm"); speak(`${route}번이 맞습니까?`);
  }
  function boardingChanged(next) {
    const previous = boardingState; boardingState = next;
    if (!running || !next) return controls();
    if (next.status === "submitted") {
      const route = normalizeRoute(next.bus_number);
      if (route !== activeRoute) {
        activeRoute = route; view.setRoute(route); view.show("search"); journey.start(route);
        if (paused) journey.pause();
      }
    } else {
      if (activeRoute) { journey.stop(); activeRoute = null; }
      if (next.status === "pending" && previous?.status !== "pending") {
        view.setRoute(draftRoute); view.show("input");
      } else if (next.status === "awaiting_stop") status("멈춤 안내가 끝나면 탑승할 버스 번호를 입력해 주세요.");
      else if (next.status === "cancelled" && previous?.status !== "cancelled") {
        view.show("walk"); status("버스 찾기를 취소했습니다. 보행 안내를 계속합니다.");
      }
    }
    controls();
  }
  function busChanged(snapshot) {
    if (!running || !activeRoute || snapshot.route !== activeRoute) return;
    const selected = snapshot.gps?.selected, arrival = selected?.arrival;
    const valid = ["ready", "tracking", "active"].includes(snapshot.gps?.status);
    const state = valid && arrival?.first_arrival_state === "arrived" ? "arrived"
      : valid && ["approaching", "arriving"].includes(arrival?.first_arrival_state) ? "approach" : "search";
    view.setBus({ route: activeRoute, station: selected?.station?.station_name || "정류장 확인 중",
      arrivalText: arrival?.first_arrival || "도착정보 확인 중", status: state,
      gpsMessage: snapshot.gps?.message || "현재 위치를 확인하고 있습니다.",
      ocrMessage: snapshot.ocr?.message || "카메라에서 버스 번호를 찾고 있습니다.", message: snapshot.gps?.message || "" });
    view.setStations((snapshot.gps?.candidates || []).map(match => ({ id: match.key,
      name: match.station?.station_name, distance_m: match.station?.distance_m,
      direction: match.arrival?.direction || match.direction })), selected?.key);
    if (!["input", "confirm", "home"].includes(view.getScreen())) view.show(state, { focus: false });
    view.setPaused(paused);
  }
  function showResult(result, capturedAt) {
    lastResult = result;
    GOverlay.render(result, state => queueTiming({ kind: "overlay", frame_id: result.frame_id,
      captured_at_ms: result.captured_at_ms, status: state,
      overlay_delay_ms: state === "drawn" ? Math.round(performance.now() - capturedAt) : null }));
    $("metrics").textContent = `${result.frame_id} 프레임 · 추론 ${result.inference_ms}ms`;
    $("stop-proximity-status").textContent = result.stop_proximity?.nearby ? "카메라에서 정류장 근접 추정" : "정류장 근접 관측 없음";
    coordinator.acceptCrosswalk(result.crosswalk?.event, capturedAt);
    coordinator.acceptWalkingSurface(result.walking_surface?.event, capturedAt);
    walking.accept({ session_id: result.session_id, frame_id: result.frame_id,
      captured_at_ms: result.captured_at_ms, detections: result.walking.detections, event: result.walking.event,
      crossing_active: result.crosswalk?.event?.crossing_active === true,
      crosswalk_status: result.crosswalk?.event?.status }, capturedAt);
    traffic.accept({ session_id: result.session_id, frame_id: result.frame_id,
      detections: result.traffic.detections, event: result.traffic.event }, capturedAt);
    boarding.accept(result, capturedAt);
    if (activeRoute) journey.accept(result.bus, result.bus?.captured_at_ms ?? result.captured_at_ms);
    view.render(result);
  }
  function scheduleFrame() {
    clearTimeout(timer);
    if (running && !paused && !document.hidden && GCamera.active()) timer = setTimeout(nextFrame, 0);
  }
  async function nextFrame() {
    if (!running || paused || document.hidden || !GCamera.active() || frameTask) return;
    const version = generation, shownVersion = presentation, id = sessionId;
    let failure = null;
    frameTask = (async () => {
      try {
        const started = performance.now();
        const blob = await GCamera.capture(settings.camera.capture_max_side, settings.camera.jpeg_quality);
        if (!blob || !running || paused || version !== generation || shownVersion !== presentation) return;
        const encodedAt = performance.now(), capturedAt = started;
        const capturedAtMs = Math.round(performance.timeOrigin + capturedAt);
        request = new AbortController();
        const sentAt = performance.now();
        const result = await GApi.frame(id, frameId + 1, capturedAtMs, blob, request.signal);
        if (!running || version !== generation) return;
        frameId = result.frame_id; // Even an in-flight paused request advances the server sequence.
        const returnedAt = performance.now();
        if (!paused && !document.hidden && shownVersion === presentation) showResult(result, capturedAt);
        queueTiming({ kind: "frame", frame_id: frameId, captured_at_ms: capturedAtMs,
          capture_ms: Math.round(encodedAt - started), round_trip_ms: Math.round(returnedAt - sentAt),
          result_ms: Math.round(performance.now() - capturedAt) });
      } catch (error) {
        if (error.name !== "AbortError" && running && version === generation) failure = error;
      } finally { if (version === generation) request = null; }
    })();
    await frameTask; frameTask = null;
    if (failure) { await stopTest(); status(`추론을 종료했습니다: ${failure.message}`); }
    else scheduleFrame();
  }
  async function startTest() {
    if (!ready || running || starting || stopping) return;
    let device = $("device").value === "custom" ? $("custom-device").value.trim() : $("device").value;
    if (device === "자동 감지") device = /iPhone/.test(navigator.userAgent) ? "iPhone"
      : /Android/.test(navigator.userAgent) ? "Android" : "웹 브라우저";
    if (!device) return status("테스트 설정에서 휴대폰 기종을 입력해 주세요.");
    starting = true;
    const version = ++generation;
    coordinator.start(); GRecorder.prepareAudio([player.recordingStream()]);
    controls(); view.show("walk"); status("카메라와 추론 모델을 준비하고 있습니다.");
    try {
      const info = await GCamera.start();
      if (version !== generation) return;
      $("camera").style.aspectRatio = `${info.width} / ${info.height}`;
      GOverlay.size(info.width, info.height); GRecorder.startPreview(); $("placeholder").hidden = true;
      const created = await GApi.start(device, $("note").value.trim());
      if (version !== generation) { await GApi.stop(created.session_id); return; }
      sessionId = created.session_id; running = true; paused = false; cameraLost = false; frameId = 0;
      activeRoute = null; draftRoute = ""; lastResult = null; timingQueue = []; eventQueue = [];
      walking.start(sessionId, false, "walking"); traffic.start(sessionId, false, "traffic"); boarding.start(sessionId);
      try { GRecorder.startRaw(); rawRecording = true; }
      catch (error) { void GApi.recordingEvent(sessionId, "camera", "failed", error.message).catch(() => {}); }
      try { GRecorder.start(); overlayRecording = true; }
      catch (error) { void GApi.recordingEvent(sessionId, "overlay", "failed", error.message).catch(() => {}); }
      tick = setInterval(() => {
        if (!paused && !document.hidden) { walking.tick(); traffic.tick(); boarding.tick(); coordinator.tick(); }
      }, settings.audio.tick_ms);
      logTimer = setInterval(() => { void flushLogs(); }, 5000);
      view.setPaused(false); status("보행 안내를 시작했습니다. 정류장에 도착하면 버튼을 눌러 주세요."); scheduleFrame();
    } catch (error) {
      if (version !== generation) return;
      if (sessionId) await stopTest();
      else { GRecorder.stopPreview(); GRecorder.cancelPreparedAudio(); GCamera.stop(); coordinator.stop(); }
      view.show("home"); status(`시작할 수 없습니다: ${error.message}`);
    } finally { if (version === generation) { starting = false; controls(); } }
  }
  function pauseTest() {
    if (!running || paused) return;
    paused = true; presentation++; lastResult = null; view.render(null); clearTimeout(timer);
    // Keep an uploaded frame alive to preserve contiguous IDs when resuming.
    journey.pause(); boarding.pause(); walking.stop(); traffic.stop(); coordinator.stop();
    GRecorder.pause(); GCamera.pause(); GOverlay.clear(); view.setPaused(true); controls();
    status("촬영·버스 조회·안내를 일시중지했습니다.");
  }
  function resumeTest() {
    if (!running || !paused) return;
    if (cameraLost) {
      paused = false; presentation++; coordinator.start(); journey.resume();
      view.setPaused(false); controls(); status("GPS 도착정보 안내를 재개했습니다. 촬영은 종료 후 다시 시작해 주세요.");
      return;
    }
    if (!GCamera.active()) return status("카메라가 종료되었습니다. 종료 후 다시 시작해 주세요.");
    presentation++; lastResult = null; view.render(null);
    paused = false; GCamera.resume(); GRecorder.resume(); player.recordingStream(); coordinator.start();
    walking.start(sessionId, false, "walking"); traffic.start(sessionId, false, "traffic"); boarding.resume(); journey.resume();
    view.setPaused(false); controls(); status("새 위치와 카메라 영상으로 안내를 재개합니다."); scheduleFrame();
  }
  async function saveRecording(id, kind, result, upload) {
    try {
      if (result.status === "rejected") throw result.reason;
      if (result.value?.size) await upload(id, result.value);
      else await GApi.recordingEvent(id, kind, "empty", "녹화된 영상이 없습니다.");
    } catch (error) {
      await GApi.recordingEvent(id, kind, "failed", String(error.message).slice(0, 500)).catch(() => {});
      return `${kind === "camera" ? "원본" : "안내"} 영상: ${error.message}`;
    }
    return null;
  }
  async function stopTest() {
    if (stopping || (!running && !starting)) return;
    stopping = true; running = false; starting = false; paused = false; automaticPause = false; ++generation;
    clearTimeout(timer); clearInterval(tick); clearInterval(logTimer); request?.abort();
    journey.stop(); boarding.stop(); walking.stop(); traffic.stop(); coordinator.stop();
    const id = sessionId;
    controls(); status("안내를 종료하고 기록을 저장하고 있습니다.");
    const recordings = await Promise.allSettled([
      rawRecording ? GRecorder.stopRaw() : Promise.resolve(null),
      overlayRecording ? GRecorder.stop() : Promise.resolve(null)]);
    GRecorder.stopPreview(); GRecorder.cancelPreparedAudio(); GCamera.stop(); GOverlay.clear(); $("placeholder").hidden = false;
    rawRecording = overlayRecording = false;
    if (frameTask) await frameTask;
    frameTask = null;
    let message = "안내를 종료했습니다.";
    if (id) {
      if (flushTask) await flushTask;
      await flushLogs(id);
      const failures = [];
      for (const [index, kind, upload] of [[0, "camera", GApi.camera], [1, "overlay", GApi.recording]]) {
        const failure = await saveRecording(id, kind, recordings[index], upload);
        if (failure) failures.push(failure);
      }
      try {
        const summary = await GApi.stop(id);
        message = `${summary.frame_count}프레임 기록을 저장했습니다.${failures.length ? ` 영상 저장 오류: ${failures.join(" / ")}` : ""}`;
      } catch (error) { message = `세션 종료 확인이 필요합니다: ${error.message}`; }
    }
    sessionId = null; activeRoute = null; lastResult = null; stopping = false;
    view.setRoute(""); view.show("home"); view.setPaused(false); controls(); status(message);
  }
  async function act(action) {
    if (!ready) return;
    if (action === "start") return startTest();
    if (action === "end") return stopTest();
    if (action === "back" && starting) return stopTest();
    if (action === "sample") return speak("길동무가 함께합니다. 선택한 속도로 안내해 드릴게요.");
    if (action === "speech-start") { coordinator.stop(); return; }
    if (action === "speech-end") { if (!paused) coordinator.start(); return; }
    if (!running || starting || stopping) return;
    if (action === "pause") { automaticPause = false; return paused ? resumeTest() : pauseTest(); }
    if (paused) return;
    if (action === "manual-arrival") {
      if (lastResult?.crosswalk?.event?.crossing_active && Date.now() - lastResult.captured_at_ms < 1500)
        return status("횡단 중에는 보행 안내를 계속합니다. 정류장에 멈춘 뒤 눌러 주세요.");
      if (boardingState?.status === "pending") return view.show("input");
      await boarding.arrive(); boarding.tick();
    } else if (action === "confirm-route") {
      if (draftRoute) await boarding.submit(draftRoute);
    } else if (action === "edit-route") {
      if (boardingState?.status === "submitted") await boarding.reopen();
      else { view.setRoute(draftRoute); view.show("input"); }
    } else if (action === "back") {
      if (view.getScreen() === "confirm") { view.show("input"); return; }
      if (activeRoute) { await boarding.reopen(); return; }
      if (["pending", "awaiting_stop"].includes(boardingState?.status)) await boarding.cancel();
      else await stopTest();
    } else if (action === "locate" && activeRoute) journey.start(activeRoute);
    else if (action === "repeat") {
      if (activeRoute) journey.repeat();
      else speak(view.getGuidance() || "정류장에 도착하면 정류장 도착 버튼을 눌러 주세요.");
    }
  }
  try {
    settings = await GConfig.load(); player = GTts.create({ onError: status }); player.setRate(preferences.rate);
    coordinator = GAudioCoordinator.create({ player, onDiagnostic: event => queueTiming({ kind: "audio",
      frame_id: event.frame_id, captured_at_ms: event.captured_at_ms, status: event.status,
      source: event.source, action: event.action ?? null, occurred_at_ms: Math.round(performance.timeOrigin + event.at_ms),
      audio_delay_ms: event.status === "started" ? Math.max(0, Math.round(performance.timeOrigin + event.at_ms - event.captured_at_ms)) : null }) });
    walking = GGuidance.create({ coordinator }); traffic = GGuidance.create({ coordinator });
    boarding = GBoarding.create({ api: GApi, coordinator, onChange: boardingChanged, onError: status });
    journey = GBusJourney.create({ api: GApi, coordinator, onChange: busChanged, onStatus: status, onEvent: queueEvent });
    ready = true; controls(); status("준비되었습니다. 시작을 눌러 주세요.");
  } catch (error) { status(`${error.message} 서버를 확인한 뒤 새로고침해 주세요.`); return; }
  $("device").addEventListener("change", () => { $("custom-device").hidden = $("device").value !== "custom"; });
  GCamera.setOnEnded(() => {
    if (!running) return;
    cameraLost = true; presentation++; lastResult = null; view.render(null); clearTimeout(timer);
    GRecorder.pause(); GOverlay.clear(); walking.stop(); traffic.stop(); boarding.pause();
    coordinator.stop(); if (!paused) coordinator.start();
    $("placeholder").hidden = false;
    status(activeRoute ? "카메라가 종료되었습니다. GPS 도착정보는 계속 확인합니다. 촬영은 종료 후 다시 시작해 주세요."
      : "카메라가 종료되었습니다. 종료 후 다시 시작해 주세요.");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && running && !paused) { automaticPause = true; pauseTest(); }
    else if (!document.hidden && automaticPause) { automaticPause = false; resumeTest(); }
  });
  window.addEventListener("pagehide", event => {
    if (event.persisted) { automaticPause = true; pauseTest(); return; }
    const id = sessionId; running = false; ++generation; request?.abort();
    clearTimeout(timer); clearInterval(tick); clearInterval(logTimer);
    journey.stop(); boarding.stop(); walking.stop(); traffic.stop(); coordinator.stop(); GCamera.stop();
    if (id) void fetch("/api/sessions/stop", { method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: id }) }).catch(() => {});
  });
})();
