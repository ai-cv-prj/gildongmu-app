/** Arrival guidance, bus input and cancellation share one session. */
(() => {
  const PROMPT = "정류장입니다. 버스를 선택하세요.";
  const ERROR_MESSAGES = new Map([
    ["횡단 중에는 버스 탑승 입력을 시작할 수 없습니다.", "횡단 중 입력 불가."],
    ["현재 정류장 도착에 해당하는 요청이 아닙니다.", "입력 상태 변경. 다시 입력."],
    ["탑승할 버스 번호를 입력해 주세요.", "번호 확인 불가. 직접 입력."],
    ["버스 번호는 1~30자의 문자로 입력해 주세요.", "번호 확인 불가. 직접 입력."],
    ["버스 번호 입력 화면을 먼저 열어 주세요.", "번호 입력 화면을 열어 주세요."],
    ["현재 취소할 버스 번호 입력이 없습니다.", "취소할 입력 없음."],
    ["확인된 정류장 도착이 없습니다.", "정류장 도착 확인 필요."],
    ["지원하지 않는 탑승 입력 요청입니다.", "입력 요청 불가."],
    ["진행 중인 세션이 없습니다.", "세션 종료됨. 안내 다시 시작."],
  ]);

  function boardingErrorMessage(error) {
    const known = ERROR_MESSAGES.get(error?.message);
    if (known) return known;
    if (error?.name === "TimeoutError" || error?.stage === "timeout"
        || error?.message === "서버 응답 시간이 초과됐습니다.") return "서버 응답 지연. 다시 시도.";
    if (!error?.status && (error?.name === "TypeError" || error?.name === "NetworkError"
        || error?.stage === "request")) return "서버 연결 불가. 다시 시도.";
    return "요청 실패. 다시 시도.";
  }

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
        if (generation === version) onError(boardingErrorMessage(error));
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
