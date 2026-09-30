/**
 * file_path: frontend/js/tts.js
 *
 * 테스트앱의 한국어 MP3 안내를 휴대폰에서 재생한다.
 */
(() => {
  const CLIPS = new Map([
    ["빨간불입니다.", "red"],
    ["초록불입니다. 다음 초록 신호를 기다려 주세요.", "green-initial-wait"],
    ["초록불입니다.", "green"],
    ["초록불로 바뀌었습니다.", "green-changed"],
    ["빨간불로 바뀌었습니다.", "red-changed"],
    ["신호를 확인할 수 없습니다.", "missing"],
    ["횡단보도 이탈! 오른쪽으로 이동하세요!", "crosswalk-exit-right"],
    ["횡단보도 이탈! 왼쪽으로 이동하세요!", "crosswalk-exit-left"],
    ["오른쪽으로 이동하세요!", "crosswalk-align-right"],
    ["왼쪽으로 이동하세요!", "crosswalk-align-left"],
  ]);
  for (const [text, name] of [...CLIPS]) CLIPS.set(`모의 신호. ${text}`, `mock-${name}`);
  const directions = [["left", "왼쪽"], ["center", "가운데"], ["right", "오른쪽"]];
  const categories = [["person", "사람"], ["vehicle", "차량"], ["obstacle", "장애물"]];
  for (const [key, direction] of directions) {
    for (const [category, name] of categories) {
      CLIPS.set(`${direction}에 ${name}.`, `danger-${key}-${category}`);
    }
    CLIPS.set(`${direction}에 여러 장애물.`, `danger-${key}-multiple`);
  }
  CLIPS.set("여러 방향에 장애물.", "danger-multiple-directions");
  const source = name => `/audio/${name}.mp3${name.startsWith("danger-")
    ? "?v=walking-direction-v1" : name.startsWith("crosswalk-")
      ? "?v=crosswalk-ava-v1" : "?v=signal-sunhi-v1"}`;

  function create({ onError = () => {}, onStatus = () => {}, now = () => performance.now() } = {}) {
    const settings = window.GConfig.get().audio;
    // 클릭으로 시작한 재생기를 이후 신호 안내에도 재사용한다.
    const audio = window.Audio ? new window.Audio() : null;
    let audioContext = null, recordingDestination = null;
    let current = null, timer = null;
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
      current = null;
      clearTimeout(timer);
      timer = null;
      if (audio && hadCurrent) {
        audio.onplaying = audio.onended = audio.onerror = null;
        audio.pause();
        // 로딩 중 취소도 실제 요청과 보류 중인 play()까지 중단한다.
        audio.removeAttribute("src");
        audio.load();
        onStatus("음성 재생을 취소했습니다.");
      }
    }

    function speak(text, validUntil = now() + settings.default_validity_ms, { onEnd = () => {} } = {}) {
      if (!audio || !CLIPS.has(text)) {
        const message = !audio ? "이 브라우저는 음성 재생을 지원하지 않습니다." : "안내 음원이 없습니다. 페이지를 새로고침해 주세요.";
        onStatus(message);
        onError(message);
        return false;
      }
      if (now() >= validUntil) return false;
      // 안내 간 우선순위와 취소는 전역 음성 관리자가 결정한다.
      // 음원 로딩 제한 시간은 앞선 안내가 끝난 뒤 재생을 시도할 때부터 센다.
      queue.push({ text, onEnd, startTimeoutMs: validUntil - now(), started: false });
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
        clearTimeout(timer);
        onStatus("음성 재생 중입니다. 들리지 않으면 미디어 음량과 연결된 이어폰을 확인해 주세요.");
        timer = setTimeout(() => fail("음성 재생이 끝나지 않아 중단했습니다. 다시 시작해 주세요."), settings.playback_timeout_ms);
      };
      const rejected = error => {
        const messages = {
          NotAllowedError: "브라우저가 음성 재생을 차단했습니다. 사이트 소리 허용을 확인하고 테스트를 다시 시작해 주세요.",
          NotSupportedError: "안내 음원을 불러오거나 재생할 수 없습니다. 페이지를 새로고침해 주세요.",
        };
        fail(messages[error?.name] || "음성을 재생하지 못했습니다. 연결 상태와 소리 설정을 확인해 주세요.");
      };
      audio.onplaying = started;
      audio.onended = () => {
        if (current !== request || !request.started || !audio.ended) return;
        clearTimeout(timer);
        timer = null;
        current = null;
        audio.onplaying = audio.onended = audio.onerror = null;
        onStatus("음성 재생이 끝났습니다.");
        request.onEnd();
        playNext();
      };
      audio.onerror = () => {
        if (!audio.error) return;
        fail(audio.error.code === 2
          ? "안내 음원을 불러오지 못했습니다. 서버 연결 상태를 확인해 주세요."
          : "안내 음원 파일을 재생할 수 없습니다. 페이지를 새로고침해 주세요.");
      };
      timer = setTimeout(() => fail("음성 재생이 지연되어 안내를 중단했습니다. 연결 상태를 확인하고 다시 시작해 주세요."),
        Math.max(0, validUntil - now()));
      try {
        const clip = CLIPS.get(request.text);
        if (audioContext?.state === "suspended") {
          audioContext.resume().catch(() => onError("브라우저가 안내 음성 재생을 차단했습니다. 사이트 소리 허용을 확인하고 테스트를 다시 시작해 주세요."));
        }
        // 위험 안내 음원은 파일 자체에 2배속을 적용했으므로 모두 기본 속도로 재생한다.
        audio.playbackRate = 1;
        audio.src = source(clip);
        audio.load();
        // await 없이 클릭 처리 중 호출해야 모바일의 사용자 동작으로 인정된다.
        const playing = audio.play();
        playing?.then(started, rejected);
        return true;
      } catch (error) {
        rejected(error);
        return false;
      }
    }

    return { speak, cancel, recordingStream };
  }
  window.GTts = { create };
})();
