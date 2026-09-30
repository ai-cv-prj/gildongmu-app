/**
 * file_path: frontend/js/app.js
 *
 * 휴대폰 카메라·추론·오버레이·음성·녹화를 하나의 테스트 흐름으로 연결한다.
 */
(() => {
  const $ = id => document.getElementById(id);
  const cameraButton = $("camera-button"), testButton = $("test-button");
  const device = $("device"), customDevice = $("custom-device");
  const status = $("status"), metrics = $("metrics"), badge = $("camera-badge");
  let sessionId = null, running = false, stopping = false, generation = 0, frameId = 0;
  let request = null, timer = null, voiceTick = null, inFlight = false;
  let pendingRawVideo = null;
  let trafficStream = null, walkingStream = null;
  const trafficPlayer = GTts.create({ onError: message => setStatus(message) });
  const walkingPlayer = GTts.create({ onError: message => setStatus(message) });
  const trafficGuide = GGuidance.create({ player: trafficPlayer });
  const walkingGuide = GGuidance.create({ player: walkingPlayer });

  // 사용자에게 현재 작업 상태 알림
  /** 화면의 단일 상태 문장을 바꾼다. */
  function setStatus(message) { status.textContent = message; }

  // 선택한 휴대폰 기종 읽기
  /** 직접 입력을 포함하여 저장할 기종 이름을 반환한다. */
  function deviceName() {
    return (device.value === "custom" ? customDevice.value : device.value).trim();
  }

  // 테스트 화면의 입력 잠금
  /** 세션 중에는 기종과 메모를 변경하지 못하게 한다. */
  function updateControls() {
    cameraButton.textContent = GCamera.active() ? "카메라 끄기" : "카메라 켜기";
    cameraButton.disabled = stopping;
    testButton.disabled = stopping || !GCamera.active();
    testButton.textContent = running ? "테스트 종료" : "테스트 시작";
    testButton.classList.toggle("stop", running);
    device.disabled = running;
    customDevice.disabled = running;
    $("note").disabled = running;
    $("record").disabled = running;
    badge.textContent = running ? "추론 중" : GCamera.active() ? "카메라 켜짐" : "대기 중";
    badge.classList.toggle("live", running);
  }

  // 카메라 권한 요청과 종료
  /** 사용자가 직접 누른 버튼에서 후면 카메라를 연다. */
  async function toggleCamera() {
    if (running) {
      await stopTest();
      return;
    }
    if (GCamera.active()) {
      GCamera.stop();
      GOverlay.clear();
      $("placeholder").hidden = false;
      setStatus("카메라를 껐습니다.");
    } else {
      try {
        const info = await GCamera.start();
        $("camera").style.aspectRatio = `${info.width} / ${info.height}`;
        GOverlay.size(info.width, info.height);
        $("placeholder").hidden = true;
        setStatus("카메라가 준비되었습니다. 테스트 시작을 누르세요.");
      } catch (error) { setStatus(error.message); }
    }
    updateControls();
  }

  // 도보와 신호의 한 프레임 결과 표시
  /** 마스크를 보여주고 두 안내 정책에 동일한 촬영 시각을 전달한다. */
  function showResult(result, capturedAt) {
    GOverlay.render(result);
    metrics.textContent = `${result.frame_id} 프레임 · ${result.inference_ms}ms`;
    walkingGuide.accept({ session_id: result.session_id, frame_id: result.frame_id,
      detections: result.walking.detections, event: result.walking.event }, capturedAt);
    trafficGuide.accept({ session_id: result.session_id, frame_id: result.frame_id,
      detections: result.traffic.detections, event: result.traffic.event }, capturedAt);
  }

  // 이전 응답을 기다린 다음 프레임 캡처
  /** 프레임 대기열을 만들지 않고 카메라의 가장 최근 장면만 전송한다. */
  async function nextFrame(version) {
    if (!running || version !== generation || document.hidden || !GCamera.active() || inFlight) return;
    inFlight = true;
    try {
      const blob = await GCamera.capture(640, 0.72);
      if (!blob || !running || version !== generation) return;
      const capturedAt = performance.now();
      const capturedAtMs = Math.round(performance.timeOrigin + capturedAt);
      const controller = new AbortController();
      request = controller;
      const result = await GApi.frame(sessionId, ++frameId, capturedAtMs, blob, controller.signal);
      if (!running || version !== generation) return;
      showResult(result, capturedAt);
      setStatus("보행 위험과 보행자 신호를 분석하고 있습니다.");
    } catch (error) {
      if (error.name !== "AbortError" && running && version === generation) {
        setStatus(`추론 오류: ${error.message}`);
        await stopTest();
      }
      return;
    } finally {
      request = null;
      inFlight = false;
    }
    if (running && version === generation) timer = setTimeout(() => nextFrame(version), 0);
  }

  // 클릭 동작에서 음성 재생 준비 후 세션 시작
  /** 서버가 준비되면 두 안내 정책과 프레임 루프를 시작한다. */
  async function startTest() {
    if (!GCamera.active()) return setStatus("먼저 카메라를 켜 주세요.");
    if (!deviceName()) return setStatus("휴대폰 기종을 입력해 주세요.");
    GOverlay.clear();
    running = true;
    frameId = 0;
    const version = ++generation;
    updateControls();
    setStatus("모델을 준비하고 있습니다. 첫 시작은 시간이 걸릴 수 있습니다.");
    try {
      pendingRawVideo = null;
      GRecorder.startRaw();
      // 모바일 오디오 정책에 따라 사용자 클릭 안에서 두 재생기를 활성화한다.
      trafficStream = trafficPlayer.recordingStream();
      walkingStream = walkingPlayer.recordingStream();
      if ($("record").checked) GRecorder.prepareAudio([trafficStream, walkingStream]);
      trafficGuide.start("pending", false, "traffic");
      walkingGuide.start("pending", false, "walking");
      const session = await GApi.start(deviceName(), $("note").value.trim());
      if (!running || version !== generation) {
        let error = null;
        try {
          const rawVideo = await pendingRawVideo;
          if (rawVideo) await GApi.camera(session.session_id, rawVideo);
          else error = new Error("원본 카메라 녹화 파일이 없습니다.");
        } catch (cause) { error = cause; }
        await GApi.stop(session.session_id).catch((cause) => { error = cause; });
        pendingRawVideo = null;
        setStatus(error ? `원본 영상 저장 실패: ${error.message}` : "원본 카메라 영상 저장 완료.");
        return;
      }
      sessionId = session.session_id;
      trafficGuide.bindSession(sessionId);
      walkingGuide.bindSession(sessionId);
      if ($("record").checked) GRecorder.start();
      voiceTick = setInterval(() => { trafficGuide.tick(); walkingGuide.tick(); }, 250);
      nextFrame(version);
    } catch (error) {
      setStatus(`테스트 시작 실패: ${error.message}`);
      await GRecorder.stop().catch(() => {});
      await GRecorder.stopRaw().catch(() => {});
      pendingRawVideo = null;
      if (sessionId) await GApi.stop(sessionId).catch(() => {});
      sessionId = null;
      running = false;
      trafficGuide.stop();
      walkingGuide.stop();
      GRecorder.cancelPreparedAudio();
      GCamera.stop();
      GOverlay.clear();
      $("placeholder").hidden = false;
      updateControls();
    }
  }

  // 추론·녹화·세션 종료
  /** 서버가 이미 처리 중인 요청도 끝날 때까지 정리한 뒤 저장한다. */
  async function stopTest() {
    if (!running || stopping) return;
    stopping = true;
    running = false;
    generation++;
    clearTimeout(timer);
    clearInterval(voiceTick);
    request?.abort();
    trafficGuide.stop();
    walkingGuide.stop();
    const id = sessionId;
    sessionId = null;
    setStatus("결과를 저장하고 있습니다.");
    let recordingError = null;
    let rawRecordingError = null;
    let video = null;
    pendingRawVideo = GRecorder.stopRaw().catch(error => {
      rawRecordingError = error;
      return null;
    });
    try {
      video = await GRecorder.stop();
    } catch (error) { recordingError = error; }
    const rawVideo = await pendingRawVideo;
    GRecorder.cancelPreparedAudio();
    GCamera.stop();
    GOverlay.clear();
    $("placeholder").hidden = false;
    updateControls();
    if (!id) {
      stopping = false;
      updateControls();
      setStatus("테스트 시작을 취소했습니다.");
      return;
    }
    try {
      if (rawVideo) await GApi.camera(id, rawVideo);
      else rawRecordingError ||= new Error("원본 카메라 녹화 파일이 없습니다.");
    } catch (error) { rawRecordingError = error; }
    try {
      if (video) await GApi.recording(id, video);
    } catch (error) { recordingError = error; }
    try {
      const summary = await GApi.stop(id);
      const errors = [rawRecordingError && `원본 영상: ${rawRecordingError.message}`,
        recordingError && `오버레이 영상: ${recordingError.message}`].filter(Boolean);
      setStatus(errors.length
        ? `${summary.frame_count}프레임 분석 완료. 영상 저장 실패: ${errors.join(" / ")}`
        : `${summary.frame_count}프레임 분석 완료. outputs/result_realtime/${summary.date}/${summary.folder_name}`);
    } catch (error) { setStatus(`세션 종료 오류: ${error.message}`); }
    finally {
      pendingRawVideo = null;
      stopping = false;
      updateControls();
    }
  }

  // 선택 사항과 브라우저 수명 이벤트 연결
  /** 화면이 숨겨지면 카메라 전송을 멈추고 다시 보이면 재개한다. */
  function bind() {
    device.addEventListener("change", () => { customDevice.hidden = device.value !== "custom"; });
    cameraButton.addEventListener("click", toggleCamera);
    testButton.addEventListener("click", () => running ? stopTest() : startTest());
    GCamera.setOnEnded(() => {
      stopTest();
      GOverlay.clear();
      setStatus("카메라 연결이 종료되었습니다.");
      updateControls();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearTimeout(timer);
        trafficGuide.interrupt();
      } else if (running) timer = setTimeout(() => nextFrame(generation), 100);
    });
    window.addEventListener("beforeunload", () => {
      request?.abort();
      if (sessionId) navigator.sendBeacon("/api/sessions/stop", new Blob(
        [JSON.stringify({ session_id: sessionId })], { type: "application/json" }));
      GCamera.stop();
    });
    updateControls();
  }

  bind();
})();
