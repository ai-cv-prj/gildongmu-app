/** Stop playback completion, bus input and cancellation share one session. */
(() => {
  const PROMPT = "정류장 근처입니다. 탑승할 버스 번호를 입력해 주세요. 보행 안내를 계속하려면 이전 화면으로 버튼을 누르세요.";

  function create({ api, coordinator, onChange = () => {}, onError = () => {},
    now = () => performance.now() }) {
    let sessionId = null, generation = 0, state = null, busy = false;
    let resultAt = null, canStop = false, audioPending = false, audioToken = 0;
    let promptFinished = false, suspended = false;
    let frameMetadata = {};
    const emit = () => onChange(state ? { ...state, busy } : null);

    function apply(next) {
      if (!next || !Number.isInteger(next.revision) || (state && next.revision < state.revision)) return;
      const previous = state?.status;
      state = { ...next };
      if (previous !== state.status) {
        audioToken++;
        audioPending = false;
        coordinator.clear("boarding-stop");
        coordinator.clear("boarding");
        if (state.status === "awaiting_stop") promptFinished = false;
      }
      emit();
    }

    async function act(action, busNumber = null) {
      if (!sessionId || (action !== "arrive" && !state?.arrival_event_id) || busy) return false;
      const version = generation, id = sessionId;
      busy = true;
      emit();
      try {
        const next = await api.boarding(id, action, state?.arrival_event_id ?? null, busNumber);
        if (generation !== version || sessionId !== id) return false;
        apply(next);
        if (action === "arrive") { resultAt = now(); canStop = true; }
        return true;
      } catch (error) {
        if (generation === version) onError(error.message);
        return false;
      } finally {
        if (generation === version) { busy = false; emit(); }
      }
    }

    function tick() {
      const manual = state?.arrival_source === "user_confirmed";
      if (!sessionId || !state || busy || suspended || audioPending
          || (!manual && (resultAt === null || now() - resultAt >= 1500))) return;
      const awaiting = state.status === "awaiting_stop";
      if ((!awaiting && (state.status !== "pending" || promptFinished)) || (awaiting && !manual && !canStop)) return;
      const version = generation, token = ++audioToken;
      audioPending = true;
      const accepted = coordinator.request({ source: awaiting ? "boarding-stop" : "boarding",
        priority: awaiting ? coordinator.PRIORITY.emergency : coordinator.PRIORITY.boarding,
        text: awaiting ? "멈추세요." : PROMPT,
        metadata: { ...frameMetadata, action: awaiting ? "stop" : null },
        validUntil: awaiting && !manual ? resultAt + 1500 : now() + 8000,
        kind: "bus-input", onCancel: () => {
          if (version === generation && token === audioToken) audioPending = false;
        }, onComplete: () => {
          if (version !== generation || token !== audioToken) return;
          audioPending = false;
          if (awaiting) void act("stop_announced");
          else promptFinished = true;
        } });
      if (!accepted) audioPending = false;
    }

    function start(id) {
      stop();
      sessionId = id;
      suspended = false;
    }
    function stop() {
      generation++;
      sessionId = state = resultAt = null;
      canStop = audioPending = busy = promptFinished = false;
      suspended = false;
      frameMetadata = {};
      audioToken++;
      coordinator.clear("boarding-stop");
      coordinator.clear("boarding");
      emit();
    }
    function accept(result, capturedAt) {
      if (!sessionId || result.session_id !== sessionId || !Number.isFinite(capturedAt)
          || now() < capturedAt || now() - capturedAt >= 1500) return;
      resultAt = capturedAt;
      frameMetadata = { frame_id: result.frame_id, captured_at_ms: result.captured_at_ms };
      canStop = (result.stop_proximity?.nearby === true || result.boarding?.obstacle_detection_enabled === false)
        && !result.crosswalk?.event?.crossing_active;
      apply(result.boarding);
      tick();
    }
    function pause() {
      suspended = true;
      audioToken++;
      audioPending = false;
      coordinator.clear("boarding-stop");
      coordinator.clear("boarding");
    }
    function resume() {
      suspended = false;
      resultAt = null;
      tick();
    }
    return { start, stop, accept, tick, submit: number => act("submit", number),
      cancel: () => act("cancel"), reopen: () => act("reopen"),
      arrive: () => act("arrive"), pause, resume };
  }
  window.GBoarding = { create, PROMPT };
})();
