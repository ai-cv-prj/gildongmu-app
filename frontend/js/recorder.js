/**
 * file_path: frontend/js/recorder.js
 *
 * 휴대폰 카메라와 통합 오버레이, 전역 안내 음성을 WebM으로 녹화한다.
 */
window.GRecorder = (() => {
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  const RECORDING_FPS = 10;
  const FRAME_INTERVAL_MS = 1000 / RECORDING_FPS;
  let recorder = null;
  let chunks = [];
  let rawRecorder = null;
  let rawChunks = [];
  let rawError = null;
  let animationId = 0;
  let lastFrameAt = null;
  let mixContext = null;
  let preparedTrack = null;

  // 사용자 클릭 중 전역 안내 음성의 녹화 트랙 준비
  /** 현장 소리 없이 전역 음성 관리자의 단일 재생 트랙만 연결한다. */
  function prepareAudio(audioStreams) {
    cancelPreparedAudio();
    const tracks = audioStreams.map(item => item?.getAudioTracks?.()[0] || null);
    preparedTrack = tracks.find(Boolean) || null;
    if (!preparedTrack) return;
    const Context = window.AudioContext || window.webkitAudioContext;
    if (Context) {
      mixContext = new Context();
      const mixed = mixContext.createMediaStreamDestination();
      tracks.forEach((track) => {
        if (!track) return;
        const source = mixContext.createMediaStreamSource(new MediaStream([track]));
        source.connect(mixed);
      });
      mixContext.resume();
      preparedTrack = mixed.stream.getAudioTracks()[0];
    }
  }

  // 녹화하지 않은 오디오 혼합기 정리
  /** 테스트 시작에 실패했을 때 열린 오디오 자원을 닫는다. */
  function cancelPreparedAudio() {
    mixContext?.close();
    mixContext = null;
    preparedTrack = null;
  }

  // 현재 브라우저가 지원하는 WebM 형식 선택
  /** MediaRecorder에서 사용할 수 있는 WebM MIME 형식을 반환한다. */
  function supportedMimeType(withAudio) {
    const formats = withAudio
      ? ["video/webm;codecs=vp8,opus", "video/webm"]
      : ["video/webm;codecs=vp8", "video/webm"];
    return formats
      .find((type) => MediaRecorder.isTypeSupported(type)) || "";
  }

  // 테스트 시작 시 원본 카메라 영상 녹화
  /** 오버레이와 마이크 트랙 없이 카메라 비디오 트랙만 녹화한다. */
  function startRaw() {
    const cameraTrack = video.srcObject?.getVideoTracks?.()[0];
    if (!window.MediaRecorder || !cameraTrack || cameraTrack.readyState !== "live") {
      throw new Error("원본 카메라 녹화를 시작할 수 없습니다.");
    }
    rawChunks = [];
    rawError = null;
    const mimeType = supportedMimeType(false);
    rawRecorder = new MediaRecorder(new MediaStream([cameraTrack]), {
      ...(mimeType ? { mimeType } : {}),
      videoBitsPerSecond: 2500000,
    });
    rawRecorder.ondataavailable = (event) => { if (event.data.size) rawChunks.push(event.data); };
    rawRecorder.onerror = (event) => { rawError = event.error || new Error("원본 카메라 녹화 실패"); };
    rawRecorder.start(1000);
  }

  // 원본 카메라 영상 녹화 종료
  /** 카메라 트랙을 끄기 전에 원본 영상 Blob을 완성한다. */
  function stopRaw() {
    if (!rawRecorder) return Promise.resolve(null);
    return new Promise((resolve, reject) => {
      const current = rawRecorder;
      current.onstop = () => {
        const blob = new Blob(rawChunks, { type: current.mimeType || "video/webm" });
        rawRecorder = null;
        rawChunks = [];
        if (rawError) reject(rawError);
        else resolve(blob.size ? blob : null);
        rawError = null;
      };
      if (current.state === "inactive") current.onstop();
      else current.stop();
    });
  }

  // 카메라와 현재 탐지 화면 합성
  /** 화면 갱신마다 호출되더라도 녹화용 합성은 30FPS 주기로만 수행한다. */
  function drawFrame(now = performance.now()) {
    if (!recorder || recorder.state === "inactive") return;
    animationId = requestAnimationFrame(drawFrame);
    if (lastFrameAt !== null) {
      const elapsed = now - lastFrameAt;
      // 33.333...ms 경계에서 부동소수점 오차로 정상 프레임이 빠지는 것을 막는다.
      const intervals = Math.floor((elapsed + 0.001) / FRAME_INTERVAL_MS);
      if (intervals < 1) return;
      // 시간 오차만 보정하고, 밀린 프레임을 한꺼번에 합성하지 않는다.
      lastFrameAt += intervals * FRAME_INTERVAL_MS;
    } else {
      lastFrameAt = now;
    }
    const width = canvas.width;
    const height = canvas.height;
    ctx.fillStyle = "#121722";
    ctx.fillRect(0, 0, width, height);

    const scale = Math.min(width / video.videoWidth, height / video.videoHeight);
    const videoWidth = video.videoWidth * scale;
    const videoHeight = video.videoHeight * scale;
    ctx.drawImage(video, (width - videoWidth) / 2, (height - videoHeight) / 2, videoWidth, videoHeight);
    ctx.drawImage(overlay, 0, 0, overlay.width, overlay.height, 0, 0, width, height);
  }

  // 실시간 탐지 화면 녹화 시작
  /** 최대 긴 변 720px, 약 10FPS로 화면과 안내 음성 녹화를 시작한다. */
  function start() {
    if (!window.MediaRecorder || !canvas.captureStream) {
      throw new Error("이 브라우저는 화면 녹화를 지원하지 않습니다.");
    }
    const displayWidth = overlay.clientWidth || video.videoWidth;
    const displayHeight = overlay.clientHeight || video.videoHeight;
    const scale = Math.min(1, 720 / Math.max(displayWidth, displayHeight));
    canvas.width = Math.max(2, Math.round(displayWidth * scale / 2) * 2);
    canvas.height = Math.max(2, Math.round(displayHeight * scale / 2) * 2);

    chunks = [];
    lastFrameAt = null;
    const stream = canvas.captureStream(RECORDING_FPS);
    const audioTrack = preparedTrack;
    if (audioTrack) stream.addTrack(audioTrack);
    const mimeType = supportedMimeType(!!audioTrack);
    recorder = new MediaRecorder(stream, {
      ...(mimeType ? { mimeType } : {}),
      videoBitsPerSecond: 2500000,
    });
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    recorder.start(1000);
    drawFrame();
  }

  // 녹화 종료 및 영상 생성
  /** 녹화를 끝내고 서버로 보낼 WebM Blob을 반환한다. */
  function stop() {
    if (!recorder || recorder.state === "inactive") return Promise.resolve(null);
    return new Promise((resolve) => {
      const current = recorder;
      current.onstop = () => {
        cancelAnimationFrame(animationId);
        const blob = new Blob(chunks, { type: current.mimeType || "video/webm" });
        recorder = null;
        chunks = [];
        cancelPreparedAudio();
        resolve(blob.size ? blob : null);
      };
      current.stop();
    });
  }

  // 측정 시점의 녹화 상태 조회
  /** 실제 MediaRecorder가 녹화 중인지 반환한다. */
  function active() {
    return recorder?.state === "recording";
  }

  return { prepareAudio, cancelPreparedAudio, startRaw, stopRaw, start, stop, active };
})();
