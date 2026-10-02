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
    let target = null, color = null, candidate = null, missingAnnounced = false;
    let hasConfirmedSignal = false;
    let mode = "traffic";
    // 대상 추적 이력과 분리한다. 소실 안내 후 재확인한 경우에만 같은 색을 다시 읽는다.
    let lastAnnouncedColor = null, lastAnnouncedTarget = null;

    function update(text) { onChange({ active, text }); }
    function resetEvidence() { target = null; color = null; candidate = null; }
    function announce(text, validUntil, metadata = {}) {
      const message = mock && mode === "traffic" ? `모의 신호. ${text}` : text;
      update(message);
      const changed = mode === "traffic" && text.includes("바뀌었습니다");
      const red = mode === "traffic" && text.startsWith("빨간불");
      return coordinator.request({ source: mode, priority: red
        ? coordinator.PRIORITY.trafficRed
        : changed ? coordinator.PRIORITY.trafficChange
          : coordinator.PRIORITY[mode], text: message, validUntil, metadata });
    }
    function stop(text = "음성 안내가 꺼져 있습니다.") {
      active = false;
      hasConfirmedSignal = false;
      lastAnnouncedColor = null;
      lastAnnouncedTarget = null;
      resetEvidence();
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
      lastFrame = lastCapture = lastValid = null;
      missingAnnounced = false;
      update(mode === "walking" ? "위험 장애물을 확인하고 있습니다." : "안내 대상의 신호를 확인하고 있습니다.");
    }
    function interrupt() {
      if (!active) return;
      if (target !== null || candidate !== null) {
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
      const age = now() - (lastValid ?? startedAt);
      if (age > audio.realtime_max_gap_ms) interrupt();
      if (hasConfirmedSignal && age >= limits.missing_ms && !missingAnnounced) {
        missingAnnounced = true;
        announce("신호를 확인할 수 없습니다.", now() + limits.max_age_ms);
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
        const danger = event.type === "walking_warning" && event.level === "danger" &&
          typeof event.voice_text === "string";
        if (!danger) return;
        announce(event.voice_text, capturedAt + limits.max_age_ms, {
          frame_id: res.frame_id, captured_at_ms: res.captured_at_ms,
          action: event.last_action,
        });
        return;
      }
      const index = event.selected_detection_index;
      const selected = Number.isInteger(index) && index >= 0 ? res.detections?.[index] : null;
      if (event.type !== "traffic_signal" || !selected || !Number.isInteger(selected.track_id) ||
          !["red", "green"].includes(event.signal_state)) {
        interrupt();
        return;
      }
      lastValid = capturedAt;
      if (target !== selected.track_id) {
        resetEvidence();
        target = selected.track_id;
        update("안내 대상의 신호를 확인하고 있습니다.");
      }
      const next = event.signal_state;
      if (!candidate || candidate.color !== next) {
        candidate = { color: next, since: capturedAt, count: 1 };
      } else candidate.count++;
      if (candidate.count < limits.stable_frames || capturedAt - candidate.since < limits.stable_ms) return;
      if (color === next) return;
      const previous = color;
      color = next;
      const firstConfirmed = !hasConfirmedSignal;
      const recoveredAfterMissing = missingAnnounced;
      // 단순 검출 후보가 아니라 색상을 안정적으로 확인한 뒤부터 소실 안내를 허용한다.
      hasConfirmedSignal = true;
      missingAnnounced = false;
      let text;
      if (previous !== null && previous !== next) {
        text = next === "green" ? "초록불로 바뀌었습니다." : "빨간불로 바뀌었습니다.";
      } else if (next === "green") {
        text = firstConfirmed ? "초록불입니다. 다음 초록 신호를 기다려 주세요."
          : "초록불입니다.";
      } else text = "빨간불입니다.";
      if (lastAnnouncedTarget === target && lastAnnouncedColor === next && !recoveredAfterMissing) {
        // 소실 안내가 없었던 짧은 끊김 뒤 같은 대상·색상은 반복하지 않는다.
        update(mock ? `모의 신호. ${text}` : text);
        return;
      }
      lastAnnouncedColor = next;
      lastAnnouncedTarget = target;
      announce(text, capturedAt + limits.max_age_ms);
    }
    // 서버 세션 생성 후 결과를 연결한다.
    function bindSession(id) { if (active) sessionId = id; }
    return { start, bindSession, stop, accept, interrupt, tick };
  }
  window.GGuidance = { create };
})();
