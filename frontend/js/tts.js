/**
 * file_path: frontend/js/tts.js
 *
 * 테스트앱의 한국어 MP3 안내를 휴대폰에서 재생한다.
 */
(() => {
  const CLIPS = new Map([
    ["빨간불.", "red"],
    ["초록불. 다음 신호까지 대기.", "green-initial-wait"],
    ["초록불.", "green"],
    ["초록불로 바뀜.", "green-changed"],
    ["빨간불로 바뀜.", "red-changed"],
    ["신호 확인 불가.", "missing"],
    ["횡단보도 이탈 오른쪽 이동!", "crosswalk-exit-right"],
    ["횡단보도 이탈 왼쪽 이동!", "crosswalk-exit-left"],
    ["오른쪽으로 이동!", "crosswalk-align-right"],
    ["왼쪽으로 이동!", "crosswalk-align-left"],
    ["왼쪽으로 한 걸음", "walking-move-left-one"],
    ["왼쪽으로 두 걸음", "walking-move-left-two"],
    ["천천히 가세요.", "walking-straight"],
    ["오른쪽으로 한 걸음", "walking-move-right-one"],
    ["오른쪽으로 두 걸음", "walking-move-right-two"],
    ["멈추세요", "walking-stop"],
    ["멈추세요.", "walking-stop"],
    ["보행로 이탈 오른쪽 이동!", "walkway-exit-right"],
    ["보행로 이탈 왼쪽 이동!", "walkway-exit-left"],
  ]);
  for (const [text, name] of [...CLIPS]) CLIPS.set(`모의 신호. ${text}`, `mock-${name}`);
  const SPEECH_TEXT = "정류장 근처입니다. 탑승할 버스 번호를 입력해 주세요. 탑승하지 않으면 취소를 누르세요.";
  const source = name => `/audio/${name}.mp3${name.startsWith("walkway-")
    ? "?v=walkway-exit-v3" : name.startsWith("walking-")
    ? "?v=walking-action-v9" : name.startsWith("crosswalk-")
      ? "?v=crosswalk-sunhi-v3" : "?v=signal-ava-v3"}`;

  function create({ onError = () => {}, onStatus = () => {}, now = () => performance.now() } = {}) {
    const settings = window.GConfig.get().audio;
    // 클릭으로 시작한 재생기를 이후 신호 안내에도 재사용한다.
    const audio = window.Audio ? new window.Audio() : null;
    let audioContext = null, recordingDestination = null;
    let current = null, timer = null;
    let rate = Math.min(2, Math.max(0.75, Number(settings.playback_rate) || 1));
    const queue = [];
    if (audio) {
      audio.preload = "auto";
      audio.volume = settings.volume;
      audio.muted = false;
    }

    // MP3 재생음을 스피커와 녹화용 오디오 트랙에 동시에 보낸다.
    /** 사용자 클릭 중 오디오 그래프를 열고 녹화에 사용할 스트림을 반환한다. */
    function recordingStream() {
      if (!audio) return null;
      if (!recordingDestination) {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) return null;
        audioContext = new Context();
        recordingDestination = audioContext.createMediaStreamDestination();
        const sourceNode = audioContext.createMediaElementSource(audio);
        sourceNode.connect(audioContext.destination);
        sourceNode.connect(recordingDestination);
      }
      // 모바일에서는 시작 버튼을 누른 동작 안에서 재생을 허용받아야 한다.
      audioContext.resume().catch(() => onError("브라우저가 안내 음성 재생을 차단했습니다. 사이트 소리 허용을 확인하고 테스트를 다시 시작해 주세요."));
      return recordingDestination.stream;
    }

    function cancel() {
      queue.length = 0;
      const hadCurrent = current !== null;
      const hadSpeech = current?.synthesized === true;
      current = null;
      clearTimeout(timer);
      timer = null;
      if (hadSpeech) window.speechSynthesis?.cancel();
      if (audio && hadCurrent && !hadSpeech) {
        audio.onplaying = audio.onended = audio.onerror = null;
        audio.pause();
        // 로딩 중 취소도 실제 요청과 보류 중인 play()까지 중단한다.
        audio.removeAttribute("src");
        audio.load();
        onStatus("음성 재생을 취소했습니다.");
      }
    }

    function speak(text, validUntil = now() + settings.default_validity_ms,
                   { onEnd = () => {}, onStart = () => {}, onFailure = () => {}, dynamic = false } = {}) {
      const synthesized = text === SPEECH_TEXT;
      const canSynthesize = window.speechSynthesis && window.SpeechSynthesisUtterance;
      const supported = synthesized ? canSynthesize : audio || (dynamic && canSynthesize);
      if (!supported || (!synthesized && !dynamic && !CLIPS.has(text))) {
        const message = synthesized
          ? "버스 번호 질문 음성을 지원하지 않는 브라우저입니다. 화면에서 입력하거나 취소해 주세요."
          : !audio ? "이 브라우저는 음성 재생을 지원하지 않습니다." : "안내 음원이 없습니다. 페이지를 새로고침해 주세요.";
        onStatus(message);
        onError(message);
        return false;
      }
      if (typeof text !== "string" || !text.trim() || text.length > 200 || now() >= validUntil) return false;
      // 안내 간 우선순위와 취소는 전역 음성 관리자가 결정한다.
      // 음원 로딩 제한 시간은 앞선 안내가 끝난 뒤 재생을 시도할 때부터 센다.
      queue.push({ text, onEnd, onStart, onFailure, synthesized, dynamic,
        startTimeoutMs: validUntil - now(), started: false });
      return current ? true : playNext();
    }

    function playNext() {
      if (current || !queue.length) return true;
      const request = queue.shift();
      const validUntil = now() + request.startTimeoutMs;
      current = request;
      onStatus("음성 재생을 준비하고 있습니다.");
      const fail = message => {
        if (current !== request) return;
        cancel();
        request.onFailure(message);
        onStatus(message);
        onError(message);
      };
      const started = () => {
        if (current !== request || request.started) return;
        if (now() >= validUntil) {
          fail("음성 재생이 지연되어 안내를 중단했습니다. 연결 상태를 확인하고 다시 시작해 주세요.");
          return;
        }
        request.started = true;
        request.onStart();
        clearTimeout(timer);
        onStatus("음성 재생 중입니다. 들리지 않으면 미디어 음량과 연결된 이어폰을 확인해 주세요.");
        timer = setTimeout(() => fail("음성 재생이 끝나지 않아 중단했습니다. 다시 시작해 주세요."), settings.playback_timeout_ms);
      };
      const finish = () => {
        if (current !== request || !request.started) return;
        clearTimeout(timer);
        timer = null;
        current = null;
        if (audio) audio.onplaying = audio.onended = audio.onerror = null;
        onStatus("음성 재생이 끝났습니다.");
        request.onEnd();
        playNext();
      };
      const synthesize = (releaseAudio = true) => {
        if (current !== request || request.fallbackAttempted || now() >= validUntil
            || !window.speechSynthesis || !window.SpeechSynthesisUtterance) return false;
        request.fallbackAttempted = true;
        request.synthesized = true;
        if (audio && releaseAudio) {
          audio.onplaying = audio.onended = audio.onerror = null;
          audio.pause();
          audio.removeAttribute("src");
          audio.load();
        }
        const utterance = new window.SpeechSynthesisUtterance(request.text);
        utterance.lang = "ko-KR";
        utterance.volume = settings.volume;
        utterance.rate = rate;
        const korean = window.speechSynthesis.getVoices().find(voice => voice.lang.startsWith("ko"));
        if (korean) utterance.voice = korean;
        utterance.onstart = started;
        utterance.onerror = () => fail("안내 음성을 재생하지 못했습니다. 화면의 안내를 확인해 주세요.");
        utterance.onend = finish;
        try { window.speechSynthesis.speak(utterance); return true; }
        catch (_) { return false; }
      };
      const rejected = error => {
        if (current !== request) return;
        // 서버 MP3를 사용할 수 없을 때 같은 우선순위 요청 안에서 한국어 합성을 시도한다.
        if (request.dynamic && !request.started && synthesize()) return true;
        const messages = {
          NotAllowedError: "브라우저가 음성 재생을 차단했습니다. 사이트 소리 허용을 확인하고 테스트를 다시 시작해 주세요.",
          NotSupportedError: "안내 음원을 불러오거나 재생할 수 없습니다. 페이지를 새로고침해 주세요.",
        };
        fail(messages[error?.name] || "음성을 재생하지 못했습니다. 연결 상태와 소리 설정을 확인해 주세요.");
        return false;
      };
      if (audio) {
        audio.onplaying = started;
        audio.onended = () => { if (audio.ended) finish(); };
        audio.onerror = () => { if (audio.error) rejected({ name: "NotSupportedError" }); };
      }
      timer = setTimeout(() => fail("음성 재생이 지연되어 안내를 중단했습니다. 연결 상태를 확인하고 다시 시작해 주세요."),
        Math.max(0, validUntil - now()));
      try {
        if (request.synthesized) {
          // 기존 번호 입력 질문은 브라우저 합성을 그대로 사용한다.
          // MP3를 재생하지 않았으므로 합성 시작 때 불필요한 pause를 피한다.
          if (audio) audio.onplaying = audio.onended = audio.onerror = null;
          return synthesize(false);
        }
        if (!audio) return synthesize();
        const clip = CLIPS.get(request.text);
        if (audioContext?.state === "suspended") {
          audioContext.resume().catch(() => onError("브라우저가 안내 음성 재생을 차단했습니다. 사이트 소리 허용을 확인하고 테스트를 다시 시작해 주세요."));
        }
        // 원본 MP3는 유지하고 테스트 재생 단계에서만 설정 속도를 적용한다.
        audio.playbackRate = rate;
        audio.src = request.dynamic && !clip
          ? `/api/bus-arrival-speech?text=${encodeURIComponent(request.text)}` : source(clip);
        audio.load();
        // await 없이 클릭 처리 중 호출해야 모바일의 사용자 동작으로 인정된다.
        const playing = audio.play();
        playing?.then(started, rejected);
        return true;
      } catch (error) {
        return rejected(error);
      }
    }

    function setRate(value) {
      const next = Number(value);
      if (Number.isFinite(next)) rate = Math.min(2, Math.max(0.75, next));
      if (audio) audio.playbackRate = rate;
      return rate;
    }

    return { speak, cancel, recordingStream, setRate, getRate: () => rate };
  }
  window.GTts = { create };
})();
