/** One camera session with independent GPS arrival and visual bus recognition. */
(async () => {
  "use strict";
  const $ = id => document.getElementById(id);
  let ready = false, settings, player, coordinator, walking, traffic, boarding, journey;
  let sessionId = null, running = false, starting = false, stopping = false, paused = false;
  let automaticPause = false, generation = 0, presentation = 0, cameraLost = false;
  let frameId = 0, timer, tick, request, frameTask;
  let boardingState = null, activeRoute = null, draftRoute = "", draftEventId = null, lastResult = null;
  let lastBusCaptureAtMs = null, lastBusResultLoggedFrameId = null;
  let busScreen = "search", beforeEnd = "walk", submittingRoute = false, routeSubmitToken = 0;
  let ratePreviewTimer = null, ratePreviewToken = 0;
  const SETTINGS_SCREENS = new Set(["home", "type"]);
  const temporaryScreen = () => SETTINGS_SCREENS.has(view.getScreen()) || view.getScreen() === "end";
  let timingQueue = [], eventQueue = [], flushTask = null, logTimer;
  const MAX_CLIPS = 5, MAX_CLIP_MS = 30000, MAX_CLIP_FRAMES = 300;
  const MAX_CLIP_FRAME_BYTES = 32 * 1024 * 1024, MAX_PENDING_BYTES = 120 * 1024 * 1024;
  const MAX_FRAME_GAP_MS = 5000;
  let clips = [], activeClip = null, clipStopTask = null, clipTimer = null, clipWatchdog = null;
  let bufferedBytes = 0, uploadSessionId = null, pendingStopSessionId = null;
  let pendingUploads = [], uploadTotalClips = 0, uploading = false;
  let pendingCheckSessionId = null, expectedClipIds = [];
  const preferenceKey = "gildongmu-accessibility-v1";
  let preferences = { rate: 1, textScale: 1 };
  try {
    const stored = JSON.parse(localStorage.getItem(preferenceKey) || "{}");
    if (Number.isFinite(stored.rate)) preferences.rate = [1, 1.5, 2].reduce((nearest, rate) =>
      Math.abs(rate - stored.rate) < Math.abs(nearest - stored.rate) ? rate : nearest);
    if ([1, 1.2, 1.5, 2].includes(stored.textScale)) preferences.textScale = stored.textScale === 2 ? 1.5 : stored.textScale;
  } catch (_) { /* Storage may be disabled. */ }
  const view = GView.create({ onAction: action => act(action),
    onSubmitRoute: submitRoute, onSpeak: text => speak(text), onStationChange: key => journey?.selectStop(key),
    onRateChange: rate => { preferences.rate = player?.setRate(rate) ?? rate; savePreferences(); previewRate(); },
    onTextScaleChange: textScale => { preferences.textScale = textScale; savePreferences(); } });
  view.setSettings(preferences); view.show("welcome", { focus: false }); view.setBusy(true);

  let diagnosticAction = null;
  window.GFetchDiagnostics?.setContextProvider(() => ({
    session_id: sessionId || pendingStopSessionId || pendingCheckSessionId || uploadSessionId,
    screen: view.getScreen(), ui_action: diagnosticAction, running,
    recording_active: Boolean(activeClip), pending_upload_count: pendingUploads.length,
  }));
  window.GFetchDiagnostics?.subscribe(summary => {
    const button = $("save-error-log"), label = $("fetch-error-log-status");
    if (button) button.hidden = summary.count === 0;
    if (label) {
      label.hidden = summary.count === 0;
      label.textContent = `연결 오류 ${summary.count}건 · 미전송 ${summary.pending}건` +
        (summary.storageAvailable ? " · 이 기기에 기록됨" : " · 창을 닫기 전에 오류 로그를 저장해 주세요");
    }
  });

  function savePreferences() {
    try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch (_) {}
  }
  function status(message) { view.setStatus(message); }
  function controls() {
    view.setBusy(!ready || starting || stopping || uploading || Boolean(boardingState?.busy));
    for (const id of ["device", "custom-device", "note"]) $(id).disabled = running || starting;
    $("camera-badge").textContent = paused ? "일시중지" : running ? "안내 중" : starting ? "준비 중" : "대기 중";
    $("camera-badge").classList.toggle("live", running && !paused);
    view.setClipState?.({ active: Boolean(activeClip), count: clips.length + (activeClip ? 1 : 0),
      max: MAX_CLIPS, available: ready && running && !starting && !stopping && !paused &&
        !cameraLost && GCamera.active() && !clipStopTask && (Boolean(activeClip) || clips.length < MAX_CLIPS) });
    $("retry-upload").hidden = (!pendingUploads.length && !pendingStopSessionId && !pendingCheckSessionId) ||
      running || starting || uploading;
    $("retry-upload").disabled = stopping || uploading;
    $("retry-upload").textContent = pendingCheckSessionId && !pendingUploads.length && !pendingStopSessionId
      ? "영상 저장 상태 다시 확인" : "영상 전송 다시 시도";
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
  function clipClock() { return Math.round(performance.timeOrigin + performance.now()); }
  function clearClipTimers() {
    clearTimeout(clipTimer); clearInterval(clipWatchdog);
    clipTimer = clipWatchdog = null;
  }
  function startClip() {
    if (!running || paused || starting || stopping || cameraLost || document.hidden ||
        !GCamera.active() || activeClip || clipStopTask) return;
    if (clips.length >= MAX_CLIPS) return status(`영상 구간은 테스트당 최대 ${MAX_CLIPS}개입니다.`);
    if (bufferedBytes >= MAX_PENDING_BYTES) return status("영상 기록 용량 120MB에 도달했습니다. 테스트를 종료해 저장해 주세요.");
    const clip = { index: clips.length + 1, frames: [], frame_bytes: 0, blob: null,
      started_at_ms: null, ended_at_ms: null, last_sent_at_ms: null, discarded: false };
    try {
      clip.started_at_ms = GRecorder.startRaw(proposedBytes => {
        return bufferedBytes + proposedBytes <= MAX_PENDING_BYTES;
      }, error => {
        if (activeClip === clip) queueMicrotask(() => {
          void finishClip(error ? `영상 기록 오류: ${error.message}` : "카메라 영상 기록이 종료됐습니다.");
        });
      }, player.recordingStream());
      clip.last_sent_at_ms = clip.started_at_ms;
      activeClip = clip;
      clipTimer = setTimeout(() => { void finishClip("30초 영상 구간 기록을 마쳤습니다."); }, MAX_CLIP_MS - 250);
      clipWatchdog = setInterval(() => {
        if (activeClip === clip && clipClock() - clip.last_sent_at_ms > MAX_FRAME_GAP_MS) {
          void finishClip("추론 프레임 전송이 멈춰 영상 기록을 마쳤습니다.");
        }
      }, 1000);
      controls();
      status(`${clip.index}번째 영상 구간 기록 중입니다. 30초 후 자동으로 끝납니다.`);
    } catch (error) { status(`영상 기록을 시작할 수 없습니다: ${error.message}`); }
  }
  async function finishClip(reason = "영상 구간 기록을 마쳤습니다.") {
    if (clipStopTask) return clipStopTask;
    const clip = activeClip;
    if (!clip) return null;
    activeClip = null;
    clearClipTimers();
    clipStopTask = (async () => {
      try {
        const recorded = await GRecorder.stopRaw();
        if (!recorded?.blob?.size) throw new Error("원본 영상이 비어 있습니다.");
        if (recorded.ended_at_ms - recorded.started_at_ms > MAX_CLIP_MS) {
          throw new Error("30초 제한을 넘긴 영상 구간은 저장하지 않습니다.");
        }
        if (bufferedBytes + recorded.blob.size > MAX_PENDING_BYTES) {
          throw new Error("영상 기록 용량 120MB에 도달했습니다.");
        }
        clip.blob = recorded.blob;
        clip.ended_at_ms = recorded.ended_at_ms;
        bufferedBytes += recorded.blob.size;
        clips.push(clip);
        status(`${reason} ${clips.length}/${MAX_CLIPS}개를 선택했습니다. 테스트 종료 후 저장합니다.`);
        return clip;
      } catch (error) {
        clip.discarded = true;
        bufferedBytes -= clip.frame_bytes;
        clip.frames = [];
        status(`영상 구간 저장 준비 실패: ${error.message}`);
        return null;
      } finally { clipStopTask = null; controls(); }
    })();
    controls();
    return clipStopTask;
  }
  function bufferClipFrame(clip, frameId, capturedAtMs, image, maskPng) {
    if (!clip || clip.discarded || capturedAtMs < clip.started_at_ms ||
        (clip.ended_at_ms !== null && capturedAtMs >= clip.ended_at_ms)) return null;
    const mask = typeof maskPng === "string" ? maskPng : "";
    const bytes = image.size + mask.length;
    if (clip.frames.length >= MAX_CLIP_FRAMES || clip.frame_bytes + bytes > MAX_CLIP_FRAME_BYTES ||
        bufferedBytes + GRecorder.bytes() + bytes > MAX_PENDING_BYTES) {
      if (activeClip === clip) void finishClip("프레임 또는 영상 용량 제한에 도달해 기록을 마쳤습니다.");
      return null;
    }
    const frame = { frame_id: frameId, captured_at_ms: capturedAtMs, image, mask_png: mask };
    clip.frames.push(frame);
    clip.frame_bytes += bytes;
    bufferedBytes += bytes;
    return frame;
  }
  function saveClipOverlay(clip, frame) {
    if (!frame || clip.discarded || clip.overlay_finalized || !clip.frames.includes(frame)) return;
    try {
      const png = GOverlay.snapshot();
      const bytes = png.length;
      if (bytes > 2 * 1024 * 1024 * 4 / 3 || clip.frame_bytes + bytes > MAX_CLIP_FRAME_BYTES ||
          bufferedBytes + GRecorder.bytes() + bytes > MAX_PENDING_BYTES) return;
      frame.overlay_png = png;
      clip.frame_bytes += bytes;
      bufferedBytes += bytes;
    } catch (error) { console.warn("화면 오버레이 저장 실패:", error); }
  }
  const clipIndex = item => Number.parseInt(String(item.clip_id).replace(/[^0-9]/g, ""), 10);
  async function pollClipStatus() {
    if (!pendingCheckSessionId || !expectedClipIds.length) return;
    for (let attempt = 0; attempt < 12; attempt++) {
      const report = await GApi.clips(pendingCheckSessionId);
      const rows = new Map((report.clips || []).map(item => [clipIndex(item), item]));
      const selected = expectedClipIds.map(id => rows.get(id));
      const failure = selected.find(item => ["failed", "no_frames"].includes(item?.state));
      if (failure) {
        status(`${failure.clip_id}번 영상 결과 생성 실패: ${failure.error || failure.state}. 서버 기록을 확인해 주세요.`);
        pendingCheckSessionId = null; expectedClipIds = [];
        return;
      }
      if (selected.every(item => item?.state === "ready")) {
        status(`${selected.length}개 구간의 원본·추론 영상 저장이 완료됐습니다.`);
        pendingCheckSessionId = null; expectedClipIds = [];
        return;
      }
      status(`원본 영상 전송 완료 · 추론 영상 생성 중 (${attempt + 1}/12)`);
      if (attempt < 11) await new Promise(resolve => setTimeout(resolve, 2000));
    }
    status("추론 영상 생성이 계속 진행 중입니다. 잠시 뒤 저장 상태를 다시 확인해 주세요.");
  }
  async function uploadQueuedClips() {
    if (uploading || (!pendingStopSessionId && !pendingUploads.length && !pendingCheckSessionId)) return;
    uploading = true; controls();
    try {
      if (pendingStopSessionId) {
        status("서버의 테스트 종료를 확인하고 있습니다.");
        try { await GApi.stop(pendingStopSessionId); }
        catch (error) {
          if (error.status !== 409) throw error;
          await GApi.clips(pendingStopSessionId);
        }
        pendingStopSessionId = null;
      }
      while (pendingUploads.length) {
        const clip = pendingUploads[0];
        // 추론 결과가 화면에 표시된 프레임만 저장한다.
        clip.overlay_finalized = true;
        clip.frames = clip.frames.filter(frame => frame.overlay_png);
        const frameBytes = clip.frames.reduce((sum, frame) =>
          sum + frame.image.size + frame.mask_png.length + frame.overlay_png.length, 0);
        bufferedBytes -= clip.frame_bytes - frameBytes;
        clip.frame_bytes = frameBytes;
        if (!clip.frames.length) {
          status(`${clip.index}번째 구간에 완료된 추론 프레임이 없어 영상 쌍을 저장하지 않았습니다.`);
          bufferedBytes -= clip.blob.size + clip.frame_bytes;
          pendingUploads.shift();
          continue;
        }
        const stored = (await GApi.clips(uploadSessionId)).clips?.find(item => clipIndex(item) === clip.index);
        if (["pending", "rendering", "ready", "failed", "no_frames"].includes(stored?.state)) {
          bufferedBytes -= clip.blob.size + clip.frame_bytes;
          pendingUploads.shift();
          continue;
        }
        status(`${clip.index}/${uploadTotalClips}번째 영상 구간을 전송하고 있습니다. 화면을 유지해 주세요.`);
        await GApi.uploadClip(uploadSessionId, clip, (done, total) => {
          status(`${clip.index}번째 영상 구간 전송 중 · ${done}/${total}`);
        });
        bufferedBytes -= clip.blob.size + clip.frame_bytes;
        pendingUploads.shift();
      }
      if (expectedClipIds.length && !pendingCheckSessionId) pendingCheckSessionId = uploadSessionId;
      uploadSessionId = null;
      if (pendingCheckSessionId) await pollClipStatus();
      else status(uploadTotalClips
        ? "완료된 추론 프레임이 없는 영상 구간은 저장하지 않았습니다."
        : "테스트 종료 기록을 저장했습니다. 선택한 영상 구간은 없습니다.");
    } catch (error) {
      status(`${pendingCheckSessionId ? "영상 저장 상태 확인" : "영상 전송"} 실패: ${error.message}. 다시 시도 버튼을 눌러 주세요.`);
    } finally { uploading = false; controls(); }
  }
  function cancelRatePreview() {
    ratePreviewToken++;
    clearTimeout(ratePreviewTimer); ratePreviewTimer = null;
    coordinator?.clear("rate-preview");
  }
  function previewRate() {
    cancelRatePreview();
    if (!ready || running || view.getScreen() !== "home") return;
    const token = ratePreviewToken;
    function play(index) {
      ratePreviewTimer = null;
      if (token !== ratePreviewToken || running || view.getScreen() !== "home") return;
      coordinator.start();
      coordinator.clear("interface");
      coordinator.request({ source: "rate-preview", priority: coordinator.PRIORITY.boarding,
        text: "왼쪽으로 한 걸음", dynamic: true, validUntil: performance.now() + 10000,
        onComplete() {
          if (index === 0 && token === ratePreviewToken && !running && view.getScreen() === "home")
            ratePreviewTimer = setTimeout(() => play(1), 360);
        } });
    }
    ratePreviewTimer = setTimeout(() => play(0), 220);
  }
  function speak(text) {
    if (!ready || !text) return;
    cancelRatePreview();
    coordinator.clear("interface");
    player.recordingStream();
    if (!running) coordinator.start();
    coordinator.request({ source: "interface", priority: coordinator.PRIORITY.boarding, text,
      dynamic: true, validUntil: performance.now() + 10000 });
  }
  const normalizeRoute = value => String(value || "").trim().replace(/\s+/g, "").replace(/번$/, "").toUpperCase();
  async function submitRoute(value) {
    // The keypad and speech input share one submission path. A late recognizer
    // or duplicate tap must not submit into another screen, stop or session.
    if (!ready || !running || starting || stopping || paused || submittingRoute ||
        boardingState?.busy || view.getScreen() !== "input") return;
    if (boardingState?.status === "awaiting_stop") {
      const message = "멈춤 안내가 끝나면 버스 찾기를 시작할 수 있어요.";
      status(message); view.setRouteError?.(message); view.announce(message);
      return;
    }
    if (boardingState?.status !== "pending") return;
    const route = normalizeRoute(value);
    if (!/^(?:[가-힣]+|[A-Z]+)?[0-9]+[A-Z]?(?:-[0-9]+)?$/.test(route) || route.length > 20) {
      status("143, N26, 마포07처럼 노선 번호를 입력해 주세요.");
      view.announce("버스 번호 형식을 확인해 주세요."); return;
    }
    const token = ++routeSubmitToken;
    submittingRoute = true;
    draftRoute = route; draftEventId = boardingState.arrival_event_id;
    view.setRoute(route);
    try { await boarding.submit(route); }
    finally { if (token === routeSubmitToken) submittingRoute = false; }
  }
  function currentGuidanceScreen() {
    if (activeRoute) return busScreen;
    return boardingState?.status === "pending" ||
      (boardingState?.status === "awaiting_stop" && boardingState.arrival_source === "user_confirmed")
      ? "input" : "walk";
  }
  function returnToGuidance() {
    view.show(currentGuidanceScreen());
    view.setPaused(paused);
  }
  const obstaclesEnabled = state => !["awaiting_stop", "pending", "submitted"].includes(state?.status);
  function boardingChanged(next) {
    const previous = boardingState; boardingState = next;
    view.setObstacleDetection(obstaclesEnabled(next));
    if (!running || !next) return controls();
    if (obstaclesEnabled(previous) !== obstaclesEnabled(next)) {
      if (!obstaclesEnabled(next)) { walking.stop(); GOverlay.clear(); }
      else if (!paused && !document.hidden) walking.start(sessionId, false, "walking");
    }
    if (next.status === "submitted") {
      const route = normalizeRoute(next.bus_number);
      if (route !== activeRoute) {
        lastBusCaptureAtMs = null;
        activeRoute = route; busScreen = "search"; void GCamera.setBusMode(true); view.setRoute(route);
        if (!temporaryScreen()) view.show("search");
        journey.start(route);
        if (paused) journey.pause();
      }
    } else {
      if (activeRoute) {
        journey.stop(); activeRoute = null; lastBusCaptureAtMs = null;
        void GCamera.setBusMode(false);
      }
      const manualWaiting = next.status === "awaiting_stop" && next.arrival_source === "user_confirmed";
      if (next.status === "pending" || manualWaiting) {
        // 새 도착에서만 초기화한다. 멈춤 안내가 끝나거나 같은 노선을 다시
        // 입력할 때는 열린 키패드와 작성 중인 번호를 그대로 유지한다.
        if (next.arrival_event_id !== draftEventId) {
          draftRoute = ""; draftEventId = next.arrival_event_id;
          view.setRoute(draftRoute);
        }
        if (next.status !== previous?.status) {
          if (next.status === "pending") view.setRouteError?.("");
          if (!temporaryScreen() && view.getScreen() !== "input") view.show("input");
          if (manualWaiting) status("멈춤 안내 중입니다. 버스 번호를 입력해 주세요.");
        }
      } else if (next.status === "awaiting_stop") {
        status("멈춤 안내가 끝나면 탑승할 버스 번호를 입력해 주세요.");
      } else if (next.status === "cancelled" && previous?.status !== "cancelled") {
        if (!temporaryScreen()) view.show("walk");
        status("버스 찾기를 취소했습니다. 보행 안내를 계속합니다.");
      }
    }
    controls();
  }
  function busChanged(snapshot) {
    if (!running || !activeRoute || snapshot.route !== activeRoute) return;
    const selected = snapshot.gps?.selected, arrival = selected?.arrival;
    const valid = ["ready", "tracking", "active"].includes(snapshot.gps?.status);
    const arrivalUnavailable = ["unavailable", "error", "denied"].includes(snapshot.gps?.status);
    const state = valid && arrival?.first_arrival_state === "arrived" ? "arrived"
      : valid && ["approaching", "arriving"].includes(arrival?.first_arrival_state) ? "approach" : "search";
    view.setBus({ route: activeRoute, station: selected?.station?.station_name || (arrivalUnavailable ? "정류장 정보 없음" : "정류장 확인 중"),
      arrivalText: arrival?.first_arrival || (arrivalUnavailable ? "도착정보 이용 불가" : "도착정보 확인 중"), status: state,
      gpsMessage: snapshot.gps?.message || "현재 위치를 확인하고 있습니다.",
      ocrMessage: snapshot.ocr?.message || "카메라에서 버스 번호를 찾고 있습니다.",
      ocrStatus: snapshot.ocr?.status || "searching", ocrCapturedAt: snapshot.ocr?.capturedAt ?? null,
      message: snapshot.gps?.message || "" });
    view.setStations((snapshot.gps?.candidates || []).map(match => ({ id: match.key,
      name: match.station?.station_name, distance_m: match.station?.distance_m,
      direction: match.arrival?.direction || match.direction })), selected?.key);
    busScreen = state;
    if (!["input", "confirm", "home", "type", "end", "welcome", "finish"].includes(view.getScreen())) view.show(state, { focus: false });
    view.setPaused(paused);
  }
  function showResult(result, capturedAt, clip = null, clipFrame = null) {
    // Apply revision-checked boarding first; an old in-flight frame must not
    // restore obstacle warnings after the user manually confirms arrival.
    boarding.accept(result, capturedAt);
    if (!obstaclesEnabled(boardingState)) result = { ...result,
      walking: { ...result.walking, detections: [], event: { enabled: false } } };
    lastResult = result;
    $("metrics").textContent = `${result.frame_id} 프레임 · 추론 ${result.inference_ms}ms`;
    $("stop-proximity-status").textContent = result.stop_proximity?.nearby ? "카메라에서 정류장 근접 추정" : "정류장 근접 관측 없음";
    coordinator.acceptCrosswalk(result.crosswalk?.event, capturedAt);
    coordinator.acceptWalkingSurface(result.walking_surface?.event, capturedAt);
    if (obstaclesEnabled(boardingState)) walking.accept({ session_id: result.session_id, frame_id: result.frame_id,
      captured_at_ms: result.captured_at_ms, detections: result.walking.detections, event: result.walking.event,
      crossing_active: result.crosswalk?.event?.crossing_active === true,
      crosswalk_status: result.crosswalk?.event?.status }, capturedAt);
    traffic.accept({ session_id: result.session_id, frame_id: result.frame_id,
      detections: result.traffic.detections, event: result.traffic.event }, capturedAt);
    if (activeRoute) journey.accept(result.bus, result.bus?.captured_at_ms ?? result.captured_at_ms);
    const overlayResult = { ...result, walking: { ...result.walking,
      event: { ...result.walking?.event,
        voice_playback_action: coordinator.walkingPlaybackAction() } } };
    GOverlay.render(overlayResult, state => {
      if (state === "drawn" || state === "stale") saveClipOverlay(clip, clipFrame);
      queueTiming({ kind: "overlay", frame_id: result.frame_id,
        captured_at_ms: result.captured_at_ms, status: state,
        overlay_delay_ms: state === "drawn" ? Math.round(performance.now() - capturedAt) : null });
    });
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
        const selectedClip = activeClip;
        const blob = await GCamera.capture(settings.camera.capture_max_side, settings.camera.jpeg_quality);
        if (!blob || !running || paused || version !== generation || shownVersion !== presentation) return;
        const encodedAt = performance.now(), capturedAt = started;
        const capturedAtMs = Math.round(performance.timeOrigin + capturedAt);
        const cameraLongSide = Math.max(GCamera.video?.videoWidth || 0, GCamera.video?.videoHeight || 0);
        let busBlob = null, busCapturedAtMs = null, busCaptureMs = null, busCaptureGapMs = null;
        let busLongSide = null, busCaptureError = null;
        const routeForFrame = activeRoute;
        if (routeForFrame && (lastBusCaptureAtMs === null ||
            capturedAtMs - lastBusCaptureAtMs >= settings.camera.bus_capture_interval_ms)) {
          const busStarted = performance.now();
          busCapturedAtMs = Math.round(performance.timeOrigin + busStarted);
          try {
            busBlob = await GCamera.capture(settings.camera.bus_capture_max_side,
              settings.camera.bus_jpeg_quality);
            if (!running || paused || version !== generation || shownVersion !== presentation ||
                activeRoute !== routeForFrame) return;
            if (busBlob) {
              busCaptureMs = Math.round(performance.now() - busStarted);
              busCaptureGapMs = lastBusCaptureAtMs === null ? null : busCapturedAtMs - lastBusCaptureAtMs;
              lastBusCaptureAtMs = busCapturedAtMs;
              busLongSide = cameraLongSide ? Math.min(settings.camera.bus_capture_max_side,
                cameraLongSide) : null;
            } else busCaptureError = "empty_frame";
          } catch (error) {
            busCaptureError = String(error?.name || "capture_error").slice(0, 80);
            console.warn("버스 프레임 캡처 실패:", error);
          }
        }
        request = new AbortController();
        const sentAt = performance.now();
        if (selectedClip && capturedAtMs >= selectedClip.started_at_ms) {
          selectedClip.last_sent_at_ms = clipClock();
        }
        const result = await GApi.frame(id, frameId + 1, capturedAtMs, blob, request.signal,
          busBlob, busCapturedAtMs);
        const clipFrame = bufferClipFrame(selectedClip, result.frame_id, capturedAtMs, blob,
          result.walking?.mask_png);
        if (!running || version !== generation) return;
        frameId = result.frame_id; // Even an in-flight paused request advances the server sequence.
        const returnedAt = performance.now();
        if (!paused && !document.hidden && shownVersion === presentation)
          showResult(result, capturedAt, selectedClip, clipFrame);
        const busTiming = {};
        if (busBlob) {
          busTiming.bus_capture_ms = busCaptureMs;
          busTiming.bus_jpeg_bytes = busBlob.size;
          if (busCaptureGapMs !== null) busTiming.bus_capture_gap_ms = busCaptureGapMs;
          if (busLongSide !== null) busTiming.bus_long_side = busLongSide;
          if (cameraLongSide) busTiming.camera_long_side = cameraLongSide;
        }
        if (busCaptureError) busTiming.bus_capture_error = busCaptureError;
        const busSourceFrameId = result.bus?.frame_id;
        if (Number.isInteger(busSourceFrameId) &&
            Number.isFinite(result.bus?.captured_at_ms) &&
            busSourceFrameId !== lastBusResultLoggedFrameId) {
          lastBusResultLoggedFrameId = busSourceFrameId;
          busTiming.bus_result_frame_id = busSourceFrameId;
          busTiming.bus_result_age_ms = Math.max(0,
            Math.round(performance.timeOrigin + returnedAt - result.bus.captured_at_ms));
        }
        queueTiming({ kind: "frame", frame_id: frameId, captured_at_ms: capturedAtMs,
          capture_ms: Math.round(encodedAt - started), round_trip_ms: Math.round(returnedAt - sentAt),
          result_ms: Math.round(performance.now() - capturedAt),
          ...busTiming,
          recording_active: Boolean(selectedClip && capturedAtMs >= selectedClip.started_at_ms &&
            (selectedClip.ended_at_ms === null || capturedAtMs < selectedClip.ended_at_ms)) });
      } catch (error) {
        if (error.name !== "AbortError" && running && version === generation) failure = error;
      } finally { if (version === generation) request = null; }
    })();
    await frameTask; frameTask = null;
    if (failure) { await stopTest(); status(`추론을 종료했습니다: ${failure.message}`); }
    else scheduleFrame();
  }
  async function startTest() {
    if (!ready || running || starting || stopping || uploading) return;
    if (pendingUploads.length || pendingStopSessionId || pendingCheckSessionId) {
      return status("이전 테스트 영상 저장 상태를 먼저 확인해 주세요.");
    }
    let device = $("device").value === "custom" ? $("custom-device").value.trim() : $("device").value;
    if (device === "자동 감지") device = /iPhone/.test(navigator.userAgent) ? "iPhone"
      : /Android/.test(navigator.userAgent) ? "Android" : "웹 브라우저";
    if (!device) return status("테스트 설정에서 휴대폰 기종을 입력해 주세요.");
    starting = true;
    const version = ++generation;
    coordinator.start();
    controls(); view.show("walk"); status("카메라와 추론 모델을 준비하고 있습니다.");
    try {
      const info = await GCamera.start();
      if (version !== generation) return;
      $("camera").style.aspectRatio = `${info.width} / ${info.height}`;
      GOverlay.size(info.width, info.height); GRecorder.startPreview(); $("placeholder").hidden = true;
      const created = await GApi.start(device, $("note").value.trim(), true);
      if (version !== generation) { await GApi.stop(created.session_id); return; }
      sessionId = created.session_id; running = true; paused = false; cameraLost = false; frameId = 0;
      activeRoute = null; lastBusCaptureAtMs = null; lastBusResultLoggedFrameId = null;
      draftRoute = ""; draftEventId = null; lastResult = null;
      timingQueue = []; eventQueue = [];
      clips = []; activeClip = null; bufferedBytes = 0; clearClipTimers();
      walking.start(sessionId, false, "walking"); traffic.start(sessionId, false, "traffic"); boarding.start(sessionId);
      tick = setInterval(() => {
        if (!paused && !document.hidden) { walking.tick(); traffic.tick(); boarding.tick(); coordinator.tick(); }
      }, settings.audio.tick_ms);
      logTimer = setInterval(() => {
        void flushLogs();
        // 일시중지 중에도 페이지가 살아 있음을 알려 강제 종료된 세션과 구분한다.
        if (sessionId) GApi.heartbeat(sessionId).catch(() => {});
      }, 5000);
      view.setPaused(false); status("보행 안내를 시작했습니다. 정류장에 도착하면 버스 번호 입력 버튼을 눌러 주세요."); scheduleFrame();
    } catch (error) {
      if (version !== generation) return;
      if (sessionId) await stopTest();
      else { GRecorder.stopPreview(); GCamera.stop(); coordinator.stop(); }
      view.show("home"); status(`시작할 수 없습니다: ${error.message}`);
    } finally { if (version === generation) { starting = false; controls(); } }
  }
  async function pauseTest() {
    if (!running || paused) return;
    paused = true; presentation++; lastResult = null; view.render(null); clearTimeout(timer);
    await finishClip("일시중지로 영상 구간을 마쳤습니다.");
    if (!running) return;
    // Keep an uploaded frame alive to preserve contiguous IDs when resuming.
    journey.pause(); boarding.pause(); walking.stop(); traffic.stop(); coordinator.stop();
    GRecorder.pause(); GCamera.pause(); GOverlay.clear(); view.setPaused(true); controls();
    status("촬영·버스 조회·안내를 일시중지했습니다.");
  }
  async function resumeTest() {
    if (!running || !paused) return;
    if (clipStopTask) await clipStopTask;
    if (!running || !paused) return;
    if (cameraLost) {
      paused = false; presentation++; coordinator.start(); journey.resume();
      view.setPaused(false); controls(); status("GPS 도착정보 안내를 재개했습니다. 촬영은 종료 후 다시 시작해 주세요.");
      return;
    }
    if (!GCamera.active()) return status("카메라가 종료되었습니다. 종료 후 다시 시작해 주세요.");
    presentation++; lastResult = null; view.render(null);
    paused = false; GCamera.resume(); GRecorder.resume(); player.recordingStream(); coordinator.start();
    if (obstaclesEnabled(boardingState)) walking.start(sessionId, false, "walking");
    traffic.start(sessionId, false, "traffic"); boarding.resume(); journey.resume();
    view.setPaused(false); controls(); status("새 위치와 카메라 영상으로 안내를 재개합니다."); scheduleFrame();
  }
  async function stopTest({ finish = false } = {}) {
    if (stopping || (!running && !starting)) return;
    stopping = true; running = false; starting = false; paused = false; automaticPause = false; ++generation;
    cancelRatePreview(); ++routeSubmitToken; submittingRoute = false;
    clearTimeout(timer); clearInterval(tick); clearInterval(logTimer);
    journey.stop(); boarding.stop(); walking.stop(); traffic.stop(); coordinator.stop();
    const id = sessionId;
    controls(); status("안내를 종료하고 기록을 저장하고 있습니다.");
    await finishClip("테스트 종료로 영상 구간을 마쳤습니다.");
    if (clipStopTask) await clipStopTask;
    GRecorder.stopPreview(); GCamera.stop(); GOverlay.clear(); $("placeholder").hidden = false;
    if (frameTask) {
      if (clips.length) {
        let timeout;
        let settled = false;
        const pending = frameTask.then(() => { settled = true; });
        await Promise.race([pending, new Promise(resolve => { timeout = setTimeout(resolve, 1500); })]);
        clearTimeout(timeout);
        if (!settled) request?.abort();
      } else request?.abort();
      await frameTask;
    }
    frameTask = null;
    let message = "안내를 종료했습니다.";
    if (id) {
      if (flushTask) await flushTask;
      await flushLogs(id);
      pendingUploads = clips;
      uploadTotalClips = clips.length;
      expectedClipIds = clips.filter(clip => clip.frames.length).map(clip => clip.index);
      uploadSessionId = id;
      pendingStopSessionId = id;
      clips = [];
      try {
        const summary = await GApi.stop(id);
        pendingStopSessionId = null;
        message = `${summary.frame_count}프레임 기록을 저장했습니다.`;
      } catch (error) { message = `세션 종료 확인이 필요합니다: ${error.message}`; }
      if (!pendingUploads.length && !pendingStopSessionId) uploadSessionId = null;
    }
    sessionId = null; activeRoute = null; lastBusCaptureAtMs = null;
    lastBusResultLoggedFrameId = null; lastResult = null; stopping = false;
    view.setRoute(""); view.show(finish ? "welcome" : "home"); view.setPaused(false); controls(); status(message);
    if (pendingUploads.length && !pendingStopSessionId) await uploadQueuedClips();
    else if (pendingStopSessionId) status(`${message} '영상 전송 다시 시도'로 종료 확인을 재시도할 수 있습니다.`);
  }
  async function act(action) {
    diagnosticAction = action;
    if (action === "save-error-log") {
      try { window.GFetchDiagnostics?.download(); }
      catch (_) { status("오류 로그 파일을 저장하지 못했습니다. 다시 시도해 주세요."); }
      return;
    }
    if (!ready) return;
    cancelRatePreview();
    if (action === "retry-upload" && !running && !starting && !stopping) return uploadQueuedClips();
    if (action === "enter" && !running && !starting && !stopping) return view.show("home");
    if (action === "finish-home" && !running && !starting && !stopping) return view.show("welcome");
    if (action === "settings-type" || action === "settings-home") {
      if (starting || stopping) return;
      view.show(action === "settings-type" ? "type" : "home");
      return;
    }
    if (action === "start") {
      if (running && SETTINGS_SCREENS.has(view.getScreen())) {
        if (paused) { automaticPause = false; await resumeTest(); }
        if (running) returnToGuidance();
        return;
      }
      return startTest();
    }
    if (action === "end" && (running || starting) && !stopping) {
      if (view.getScreen() !== "end") beforeEnd = view.getScreen();
      view.show("end"); return;
    }
    if (action === "cancel-end" && view.getScreen() === "end" && !stopping) {
      if (SETTINGS_SCREENS.has(beforeEnd)) view.show(beforeEnd);
      else returnToGuidance();
      return;
    }
    if (action === "confirm-end" && view.getScreen() === "end") return stopTest({ finish: true });
    if (action === "back" && starting) return stopTest();
    if (action === "sample") return speak("길동무가 함께합니다. 선택한 속도로 안내해 드릴게요.");
    if (action === "speech-start") { coordinator.stop(); return; }
    if (action === "speech-end") { if (!paused) coordinator.start(); return; }
    if (!running || starting || stopping) return;
    if (action === "record") return activeClip ? finishClip() : startClip();
    if (action === "pause") { automaticPause = false; return paused ? resumeTest() : pauseTest(); }
    // 이전은 일시중지 중에도 화면을 벗어날 수 있어야 한다.
    if (paused && action !== "back") return;
    if (action === "manual-arrival") {
      if (lastResult?.crosswalk?.event?.crossing_active && Date.now() - lastResult.captured_at_ms < 1500)
        return status("횡단 중에는 보행 안내를 계속합니다. 정류장에 멈춘 뒤 눌러 주세요.");
      if (boardingState?.status === "pending") return view.show("input");
      await boarding.arrive(); boarding.tick();
    } else if (action === "confirm-route") {
      const route = view.readRoute?.() ?? draftRoute;
      if (route) await submitRoute(route);
    } else if (action === "edit-route") {
      if (boardingState?.status === "submitted") { await boarding.reopen(); boarding.tick(); }
      else { view.setRoute(draftRoute); view.show("input"); }
    } else if (action === "back") {
      if (view.getScreen() === "confirm") { view.show("input"); return; }
      if (activeRoute) {
        // 노선 입력 화면의 확인은 일시중지 중에 막히므로 안내를 재개한 뒤 다시 연다.
        if (paused) await resumeTest();
        if (!paused) { await boarding.reopen(); boarding.tick(); }
        return;
      }
      if (["pending", "awaiting_stop"].includes(boardingState?.status)) await boarding.cancel();
      else await stopTest();
    } else if (action === "locate" && activeRoute) journey.start(activeRoute);
    else if (action === "repeat") {
      if (activeRoute) journey.repeat();
      else speak(view.getGuidance() || "정류장에 도착하면 버스 번호 입력 버튼을 눌러 주세요.");
    }
  }
  try {
    settings = await GConfig.load(); player = GTts.create({ onError: status }); player.setRate(preferences.rate);
    coordinator = GAudioCoordinator.create({ player, onDiagnostic: event => queueTiming({ kind: "audio",
      frame_id: event.frame_id, captured_at_ms: event.captured_at_ms, status: event.status,
      source: event.source, action: event.action ?? null, occurred_at_ms: Math.round(performance.timeOrigin + event.at_ms),
      audio_delay_ms: event.status === "started" ? Math.max(0, Math.round(performance.timeOrigin + event.at_ms - event.captured_at_ms)) : null }) });
    walking = GGuidance.create({ coordinator }); traffic = GGuidance.create({ coordinator });
    boarding = GBoarding.create({ api: GApi, coordinator, onChange: boardingChanged, onError: message => {
      status(message);
      if (view.getScreen() === "input") { view.setRouteError?.(message); speak(message); }
    } });
    journey = GBusJourney.create({ api: GApi, coordinator, onChange: busChanged, onStatus: status, onEvent: queueEvent });
    ready = true; controls(); status("준비되었습니다. 시작하기를 눌러 주세요.");
  } catch (error) { status(`${error.message} 서버를 확인한 뒤 새로고침해 주세요.`); return; }
  $("device").addEventListener("change", () => { $("custom-device").hidden = $("device").value !== "custom"; });
  GCamera.setOnExposureChange(report => {
    const text = {
      stopped: "카메라 종료 · 다음 시작 시 기본 촬영",
      default: "기본 촬영 · 버스 번호 확정 후 LED 촬영 설정 시도",
      applying: "버스 LED 촬영 설정 적용 중",
      applied: `버스 LED 촬영 설정 적용 · 노출 약 ${((report.actual_us || 0) / 1000).toFixed(2)}ms`,
      unsupported: report.reason === "disabled" ? "기본 촬영 · LED 촬영 설정 꺼짐"
        : "기본 촬영 · 이 기기에서는 LED 촬영 설정을 지원하지 않음",
      failed: "기본 촬영으로 복원 · LED 촬영 설정 적용 확인 실패",
      restoring: "버스 찾기 종료 · 이전 촬영 설정 복원 중",
      restore_failed: "촬영 설정 복원을 확인할 수 없어 카메라를 종료했습니다. 다시 시작해 주세요.",
    }[report.status] || "카메라 촬영 설정 확인 중";
    $("camera-exposure-status").textContent = text;
    queueEvent({ type: "camera_exposure", occurred_at_ms: Date.now(), ...report });
  });
  GCamera.setOnEnded(() => {
    if (!running) return;
    cameraLost = true; presentation++; lastResult = null; view.render(null); clearTimeout(timer);
    void finishClip("카메라 종료로 영상 구간을 마쳤습니다.");
    GRecorder.pause(); GOverlay.clear(); walking.stop(); traffic.stop(); boarding.pause();
    coordinator.stop(); if (!paused) coordinator.start();
    $("placeholder").hidden = false;
    status(activeRoute ? "카메라가 종료되었습니다. GPS 도착정보는 계속 확인합니다. 촬영은 종료 후 다시 시작해 주세요."
      : "카메라가 종료되었습니다. 종료 후 다시 시작해 주세요.");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelRatePreview();
    if (document.hidden && running && !paused) { automaticPause = true; pauseTest(); }
    else if (!document.hidden && automaticPause) { automaticPause = false; resumeTest(); }
  });
  window.addEventListener("pagehide", event => {
    cancelRatePreview();
    if (event.persisted) { automaticPause = true; pauseTest(); return; }
    const finalizingClip = finishClip("페이지가 닫혀 영상 기록을 마쳤습니다.");
    const id = sessionId; running = false; ++generation; request?.abort();
    clearTimeout(timer); clearInterval(tick); clearInterval(logTimer);
    journey.stop(); boarding.stop(); walking.stop(); traffic.stop(); coordinator.stop();
    void Promise.resolve(finalizingClip).finally(() => GCamera.stop());
    const stopFetch = window.GFetchDiagnostics?.fetch || fetch;
    if (id) void stopFetch("/api/sessions/stop", { method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: id }) }).catch(() => {});
  });
})();
