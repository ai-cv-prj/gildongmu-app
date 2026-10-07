/** Arrival guidance, bus input and cancellation share one session. */
(() => {
  const PROMPT = "정류장입니다. 버스를 선택하세요.";

  function create({ api, coordinator, onChange = () => {}, onError = () => {},
    now = () => performance.now() }) {
    let sessionId = null, generation = 0, state = null, busy = false;
    let resultAt = null, canEnter = false, audioPending = false, audioToken = 0;
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
        if (action === "arrive") { resultAt = now(); canEnter = true; }
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
      if ((!awaiting && (state.status !== "pending" || promptFinished)) || (awaiting && !manual && !canEnter)) return;
      if (awaiting) {
        // Open input without waiting for arrival audio to finish.
        void act("input_ready").then(accepted => { if (accepted) tick(); });
        return;
      }
      const version = generation, token = ++audioToken;
      audioPending = true;
      const accepted = coordinator.request({ source: "boarding",
        priority: coordinator.PRIORITY.boarding,
        text: PROMPT,
        metadata: { ...frameMetadata, action: null },
        validUntil: now() + 8000,
        kind: "bus-input", onCancel: () => {
          if (version === generation && token === audioToken) audioPending = false;
        }, onComplete: () => {
          if (version !== generation || token !== audioToken) return;
          audioPending = false;
          promptFinished = true;
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
      canEnter = audioPending = busy = promptFinished = false;
      suspended = false;
      frameMetadata = {};
      audioToken++;
      coordinator.clear("boarding");
      emit();
    }
    function accept(result, capturedAt) {
      if (!sessionId || result.session_id !== sessionId || !Number.isFinite(capturedAt)
          || now() < capturedAt || now() - capturedAt >= 1500) return;
      resultAt = capturedAt;
      frameMetadata = { frame_id: result.frame_id, captured_at_ms: result.captured_at_ms };
      canEnter = (result.stop_proximity?.nearby === true || result.boarding?.obstacle_detection_enabled === false)
        && !result.crosswalk?.event?.crossing_active;
      apply(result.boarding);
      tick();
    }
    function pause() {
      suspended = true;
      audioToken++;
      audioPending = false;
      coordinator.clear("boarding");
    }
    function dismissPrompt() {
      promptFinished = true;
      audioToken++;
      audioPending = false;
      coordinator.clear("boarding");
    }
    function resume() {
      suspended = false;
      resultAt = null;
      tick();
    }
    return { start, stop, accept, tick, submit: number => act("submit", number),
      cancel: () => act("cancel"), reopen: async () => {
        const reopened = await act("reopen");
        if (reopened) dismissPrompt();
        return reopened;
      },
      arrive: () => act("arrive"), pause, resume, dismissPrompt };
  }
  window.GBoarding = { create, PROMPT };
})();
