/**
 * file_path: frontend/js/guidance.js
 *
 * 테스트앱과 같은 신호 상태 및 보행 위험 장애물 음성 안내 정책.
 */
(() => {
  function create({ coordinator, onChange = () => {}, now = () => performance.now() }) {
    const audio = window.GConfig.get().audio;
    const limits = audio.guidance;
    let active = false, sessionId = null, mock = false;
    let startedAt = 0, lastFrame = null, lastCapture = null, lastValid = null;
    // samples는 현재 대상의 최근 색상 관측이며, 인식불가 프레임은 넣지 않는다.
    let target = null, color = null, samples = [], missingAnnounced = false;
    // 대상 추적이 잠깐 끊겨도 직전에 확정한 대상·색상을 기억해 전환과 다른 신호의 초록을 구분한다.
    let remembered = null;
    let hasConfirmedSignal = false;
    let trafficAllowed = false;
    let mode = "traffic";
    // 대상 추적 이력과 분리한다. 소실 안내 후 재확인한 경우에만 같은 색을 다시 읽는다.
    let lastAnnouncedColor = null, lastAnnouncedTarget = null;
    // 같은 대상·색상이 계속 확인되면 마지막 신호 안내 후 일정 시간마다 다시 읽는다.
    let lastAnnouncedAt = null, repeatText = null;
    // 다른 음성에 밀려 재생하지 못한 신호 안내는 같은 색상이 유지되는 동안 다시 요청한다.
    let retryText = null;
    // 횡단 중 파란 ROI의 횡단보도 근거가 처음 끊긴 시각이다. 다시 허용되면 지운다.
    let gateLostAt = null;
    let lastWalkingAction = null;
    let lastWalkingEvent = null;

    function update(text) { onChange({ active, text }); }
    function resetEvidence() { target = null; color = null; samples = []; retryText = null; }
    // 횡단보도 근거가 없으면 신호 음성과 이전 색상 기억을 해제한다.
    /** 다시 허용될 때 새 신호처럼 안정화하며 다른 출처의 음성은 유지한다. */
    function suspendTraffic() {
      trafficAllowed = false;
      hasConfirmedSignal = missingAnnounced = false;
      lastAnnouncedColor = lastAnnouncedTarget = lastValid = remembered = null;
      lastAnnouncedAt = repeatText = null;
      resetEvidence();
      coordinator.clear("traffic");
    }
    // 횡단 중 횡단보도 근거가 잠깐 끊기면 신호 음성만 멈추고 확인한 신호 기억은 유지한다.
    /** 다시 허용될 때 이미 확인한 신호를 처음 본 신호처럼 대기 안내하지 않는다. */
    function holdTraffic(capturedAt) {
      trafficAllowed = false;
      // 끊긴 동안은 소실 시간에 포함하지 않는다.
      lastValid = capturedAt;
      resetEvidence();
      coordinator.clear("traffic");
    }
    function announce(text, validUntil, metadata = {}) {
      const message = mock && mode === "traffic" ? `모의 신호. ${text}` : text;
      update(message);
      // 같은 색상 재안내는 보행 안내를 끊지 않도록 가장 낮은 신호 우선순위를 쓴다.
      const repeat = metadata.repeat === true;
      const changed = mode === "traffic" && !repeat && text.includes("바뀜");
      const red = mode === "traffic" && !repeat && text.startsWith("빨간불");
      return coordinator.request({ source: mode, priority: red
        ? coordinator.PRIORITY.trafficRed
        : changed ? coordinator.PRIORITY.trafficChange
          : metadata.action === "stop" ? coordinator.PRIORITY.emergency ?? 0
          : metadata.missing === true ? coordinator.PRIORITY.trafficMissing ?? coordinator.PRIORITY[mode]
          : coordinator.PRIORITY[mode], text: message, validUntil, metadata });
    }
    // 재생이 수락된 신호 안내만 마지막 안내로 기록한다.
    function announceSignal(text, capturedAt) {
      if (!announce(text, capturedAt + limits.max_age_ms)) {
        retryText = text;
        return;
      }
      retryText = null;
      lastAnnouncedColor = color;
      lastAnnouncedTarget = target;
      lastAnnouncedAt = capturedAt;
      // 전환 안내 뒤에는 현재 색상만, 첫 초록 대기 안내는 같은 문구로 다시 읽는다.
      repeatText = text.includes("바뀜") ? (color === "green" ? "초록불" : "빨간불") : text;
    }
    function stop(text = "음성 안내가 꺼져 있습니다.") {
      active = false;
      hasConfirmedSignal = false;
      trafficAllowed = false;
      lastAnnouncedColor = null;
      lastAnnouncedTarget = null;
      lastAnnouncedAt = repeatText = remembered = null;
      resetEvidence();
      lastWalkingAction = null;
      lastWalkingEvent = null;
      coordinator.clear(mode);
      update(text);
    }
    function start(id, isMock = false, nextMode = "traffic") {
      stop();
      active = true;
      mode = nextMode;
      sessionId = id;
      mock = isMock;
      startedAt = now();
      lastFrame = lastCapture = lastValid = gateLostAt = null;
      missingAnnounced = false;
      update(mode === "walking" ? "위험 장애물을 확인하고 있습니다." : "안내 대상의 신호를 확인하고 있습니다.");
    }
    function interrupt() {
      if (!active) return;
      if (target !== null || samples.length) {
        resetEvidence();
        update("신호를 다시 확인하고 있습니다.");
      }
    }
    // 보행 위험 장면이 끝나면 지난 음성을 정리하고 신호 안내의 관측 정책은 유지한다.
    function tick() {
      if (!active) return;
      if (mode === "walking") {
        return;
      }
      if (!trafficAllowed) return;
      if (lastCapture === null || now() - lastCapture >= limits.max_age_ms) {
        suspendTraffic();
        return;
      }
      const age = now() - (lastValid ?? startedAt);
      if (age > audio.realtime_max_gap_ms) interrupt();
      if (hasConfirmedSignal && age >= limits.missing_ms && !missingAnnounced) {
        missingAnnounced = true;
        // 소실을 알린 뒤에는 이전 관측을 버리고 다시 확정한 색상을 안내한다.
        resetEvidence();
        announce("신호 확인 불가", now() + limits.max_age_ms, { missing: true });
      }
    }
    function accept(res, capturedAt) {
      if (!active || res.session_id !== sessionId || capturedAt < startedAt) return;
      const time = now();
      if (!Number.isFinite(capturedAt) || time < capturedAt || time - capturedAt >= limits.max_age_ms) {
        interrupt();
        return;
      }
      if (!Number.isInteger(res.frame_id) || (lastFrame !== null && res.frame_id <= lastFrame)) return;
      const continuous = lastFrame === null || (res.frame_id === lastFrame + 1 &&
        capturedAt > lastCapture && capturedAt - lastCapture <= audio.realtime_max_gap_ms);
      if (!continuous) interrupt();
      lastFrame = res.frame_id;
      lastCapture = capturedAt;
      const event = res.event || {};
      if (mode === "walking") {
        const vehicleOnly = res.crossing_active || res.crosswalk_status === "approach";
        const voice = event.voice_event;
        if (voice && typeof voice.text === "string"
            && ["left", "right", "crowded", "blocked", "stop"].includes(voice.action)) {
          if ((voice.action !== lastWalkingAction || Number.isInteger(voice.event_id)
              && voice.event_id !== lastWalkingEvent) && announce(voice.text, capturedAt + limits.max_age_ms, {
            frame_id: res.frame_id, captured_at_ms: res.captured_at_ms, action: voice.action,
          })) { lastWalkingAction = voice.action; lastWalkingEvent = voice.event_id; }
          return;
        }
        if (event.voice_action === null && (event.voice_clear !== false || vehicleOnly)) {
          lastWalkingAction = null;
          lastWalkingEvent = null;
          coordinator.clear(mode);
        }
        const danger = event.type === "walking_warning" && event.level === "danger" &&
          typeof event.voice_text === "string"
          && ["left", "right", "crowded", "blocked", "stop"].includes(event.last_action);
        if (!danger) return;
        announce(event.voice_text, capturedAt + limits.max_age_ms, {
          frame_id: res.frame_id, captured_at_ms: res.captured_at_ms,
          action: event.last_action,
        });
        return;
      }
      if (event.voice_gate?.allowed !== true) {
        gateLostAt ??= capturedAt;
        // 건너도 되는 초록불을 안내하고 건너는 중일 때만 유지한다.
        // 빨간불이나 대기 안내 뒤에는 기존처럼 새로 확인해 대기 안내를 이어 간다.
        if (res.crossing_active === true && repeatText === "초록불"
            && capturedAt - gateLostAt < limits.traffic_crossing_hold_ms) holdTraffic(capturedAt);
        else suspendTraffic();
        update("횡단보도 접근을 확인하고 있습니다.");
        return;
      }
      gateLostAt = null;
      trafficAllowed = true;
      const index = event.selected_detection_index;
      const selected = Number.isInteger(index) && index >= 0 ? res.detections?.[index] : null;
      // 인식불가 프레임은 색상 다수결에서 건너뛰고, 긴 공백은 tick의 소실 기준으로 정리한다.
      if (event.type !== "traffic_signal" || !selected || !Number.isInteger(selected.track_id) ||
          !["red", "green"].includes(event.signal_state)) return;
      lastValid = capturedAt;
      if (target !== selected.track_id) {
        resetEvidence();
        target = selected.track_id;
        update("안내 대상의 신호를 확인하고 있습니다.");
      }
      const next = event.signal_state;
      // 최근 관측 중 같은 색이 기준 개수 이상이면 순간 오인식이 섞여도 색상을 확정한다.
      samples.push({ color: next, at: capturedAt });
      if (samples.length > limits.stable_window_frames) samples.shift();
      const votes = samples.filter(item => item.color === next);
      if (votes.length < limits.stable_frames || capturedAt - votes[0].at < limits.stable_ms) return;
      const memory = remembered && capturedAt - remembered.at <= limits.traffic_color_memory_ms
        ? remembered : null;
      remembered = { target, color: next, at: capturedAt };
      if (color === next) {
        if (retryText !== null) {
          announceSignal(retryText, capturedAt);
          return;
        }
        if (repeatText !== null && lastAnnouncedTarget === target && lastAnnouncedColor === next
            && capturedAt - lastAnnouncedAt >= limits.traffic_repeat_ms) {
          lastAnnouncedAt = capturedAt;
          announce(repeatText, capturedAt + limits.max_age_ms, { repeat: true });
        }
        return;
      }
      // 같은 대상이 짧게 끊긴 뒤 다른 색으로 확인되면 전환으로 안내한다.
      const previous = color ?? (memory?.target === target ? memory.color : null);
      // 빨간불을 보던 중 다른 신호등의 초록을 잡으면 바뀌는 순간을 보지 못한 초록으로 본다.
      const unverifiedGreen = next === "green" && memory?.target !== target && memory?.color === "red";
      color = next;
      const firstConfirmed = !hasConfirmedSignal;
      const recoveredAfterMissing = missingAnnounced;
      // 단순 검출 후보가 아니라 색상을 안정적으로 확인한 뒤부터 소실 안내를 허용한다.
      hasConfirmedSignal = true;
      missingAnnounced = false;
      let text;
      if (previous !== null && previous !== next) {
        text = next === "green" ? "초록불로 바뀜" : "빨간불로 바뀜";
      } else if (next === "green") {
        text = firstConfirmed || unverifiedGreen ? "초록불, 다음 신호까지 대기"
          : "초록불";
      } else text = "빨간불";
      if (lastAnnouncedTarget === target && lastAnnouncedColor === next && !recoveredAfterMissing) {
        // 소실 안내가 없었던 짧은 끊김 뒤 같은 대상·색상은 반복하지 않는다.
        update(mock ? `모의 신호. ${text}` : text);
        return;
      }
      announceSignal(text, capturedAt);
    }
    // 서버 세션 생성 후 결과를 연결한다.
    function bindSession(id) { if (active) sessionId = id; }
    return { start, bindSession, stop, accept, interrupt, tick };
  }
  window.GGuidance = { create };
})();
