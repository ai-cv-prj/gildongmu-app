/** Browser speech input. A spoken route is always reviewed before submission. */
(() => {
  const DIGITS = { 영: 0, 공: 0, 일: 1, 이: 2, 삼: 3, 사: 4, 오: 5, 육: 6, 칠: 7, 팔: 8, 구: 9 };
  const UNITS = { 십: 10, 백: 100, 천: 1000 };

  function spokenNumber(value) {
    if (!/[십백천]/.test(value)) return [...value].map(char => DIGITS[char]).join("");
    let total = 0, digit = 0;
    for (const char of value) {
      if (char in UNITS) { total += (digit || 1) * UNITS[char]; digit = 0; }
      else digit = DIGITS[char];
    }
    return String(total + digit);
  }

  function normalizeRouteTranscript(value) {
    return String(value || "").normalize("NFKC").trim()
      .replace(/^(?:버스\s*번호|노선\s*번호|버스|노선)\s*/, "")
      .replace(/\s*번(?:\s*버스)?[.!?。]?$/, "")
      .replace(/\s*버스[.!?。]?$/, "")
      .replace(/하이픈/g, "-")
      .replace(/^엔(?=[영공일이삼사오육칠팔구십백천0-9\s])/, "N")
      .split(/(\s+)/).map(part => {
        if (/^[영공일이삼사오육칠팔구십백천]+$/.test(part)) return spokenNumber(part);
        const prefixed = part.match(/^([A-Za-z]+)([영공일이삼사오육칠팔구십백천]+)$/);
        return prefixed ? prefixed[1] + spokenNumber(prefixed[2]) : part;
      }).join("")
      .replace(/\s+/g, "").replace(/[.!?。]+$/, "").slice(0, 30);
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
        onError("이 브라우저는 음성 입력을 지원하지 않습니다. 번호를 입력하거나 화면의 버튼을 눌러 주세요.");
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
            onError(mode === "confirm" ? "답변을 확인하지 못했어요. 맞아요 또는 다시 입력을 눌러 주세요." : "번호를 확인하지 못했어요. 다시 말하거나 직접 입력해 주세요.");
          } else onResult({ mode, value, transcript });
        };
        next.onerror = event => {
          if (token !== generation || event.error === "aborted") return;
          handled = true;
          const messages = {
            "not-allowed": "마이크 권한이 필요합니다. 브라우저 설정에서 허용하거나 번호를 직접 입력해 주세요.",
            "service-not-allowed": "이 브라우저에서는 음성 입력을 사용할 수 없습니다. 번호를 직접 입력해 주세요.",
            "audio-capture": "마이크를 사용할 수 없습니다. 번호를 직접 입력해 주세요.",
            "no-speech": "음성이 들리지 않았어요. 다시 말하거나 번호를 직접 입력해 주세요.",
            network: "음성 인식 서비스에 연결하지 못했어요. 번호를 직접 입력해 주세요.",
          };
          onError(messages[event.error] || "음성 입력을 완료하지 못했어요. 화면에서 입력해 주세요.");
        };
        next.onend = () => {
          if (token !== generation) return;
          recognition = null;
          active = false;
          onState(false, mode);
          if (!handled) onError("음성을 확인하지 못했어요. 다시 말하거나 화면에서 입력해 주세요.");
        };
        next.start();
        active = true;
        onState(true, mode);
        return true;
      } catch (_) {
        stop();
        onError("음성 입력을 시작하지 못했어요. 번호를 직접 입력해 주세요.");
        return false;
      }
    }
    return { start, stop, supported: Boolean(Recognition), isActive: () => active, destroy: stop };
  }
  window.GSpeechInput = { create, normalizeRouteTranscript, confirmation };
})();
