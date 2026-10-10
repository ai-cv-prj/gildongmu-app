/** Browser speech input; only a validated route is handed to the application. */
(() => {
  function normalizeRouteTranscript(value) {
    return window.GRouteKeypad.normalize(value);
  }

  function confirmation(value) {
    const text = String(value || "").replace(/\s/g, "");
    if (/다시|아니|안맞|맞지않|틀렸|틀려|수정|취소/.test(text)) return "edit-route";
    if (/^(네|예|응)[.!?]?$/.test(text) || /맞아|맞습|확인|맞네/.test(text)) return "confirm-route";
    return null;
  }

  function create({ onState = () => {}, onResult = () => {}, onError = () => {} } = {}) {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recognition = null, generation = 0, active = false;
    function stop() {
      generation++;
      const previous = recognition;
      recognition = null;
      active = false;
      if (previous) {
        previous.onstart = previous.onresult = previous.onerror = previous.onend = null;
        try { previous.abort(); } catch (_) { /* Already stopped. */ }
      }
      onState(false);
    }
    function start(mode = "input") {
      stop();
      if (!Recognition) {
        onError("음성 입력 지원 안함. 번호 직접 입력.");
        return false;
      }
      const token = generation;
      let handled = false;
      try {
        const next = new Recognition();
        recognition = next;
        next.lang = "ko-KR";
        next.continuous = false;
        next.interimResults = false;
        next.maxAlternatives = 1;
        next.onstart = () => {
          if (token !== generation) return;
          active = true;
          onState(true, mode);
        };
        next.onresult = event => {
          if (token !== generation || handled) return;
          const result = event.results[event.resultIndex || 0];
          const transcript = result?.[0]?.transcript?.trim();
          if (!transcript || result.isFinal === false) return;
          handled = true;
          const value = mode === "confirm" ? confirmation(transcript) : normalizeRouteTranscript(transcript);
          if (!value) {
            onError(mode === "confirm" ? "답변 확인 불가. 맞아요 또는 다시 입력." : "번호 확인 불가. 다시 말하거나 직접 입력.");
          } else onResult({ mode, value, transcript });
        };
        next.onerror = event => {
          if (token !== generation || event.error === "aborted") return;
          handled = true;
          const messages = {
            "not-allowed": "마이크 권한 필요.",
            "service-not-allowed": "음성 인식 불가.",
            "audio-capture": "마이크 사용 불가. 번호 직접 입력.",
            "no-speech": "음성 감지 안됨. 다시 말하거나 직접 입력.",
            network: "음성 인식 불가.",
          };
          onError(messages[event.error] || "음성 인식 불가.");
        };
        next.onend = () => {
          if (token !== generation) return;
          recognition = null;
          active = false;
          onState(false, mode);
          if (!handled) onError("음성 확인 불가. 다시 말하거나 직접 입력.");
        };
        next.start();
        active = true;
        onState(true, mode);
        return true;
      } catch (_) {
        stop();
        onError("음성 입력 시작 불가. 번호 직접 입력.");
        return false;
      }
    }
    return { start, stop, supported: Boolean(Recognition), isActive: () => active, destroy: stop };
  }
  window.GSpeechInput = { create, normalizeRouteTranscript, confirmation };
})();
