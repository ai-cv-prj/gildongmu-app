/** Camera preview and optional short raw-camera recording. */
window.GRecorder = (() => {
  const MAX_CLIP_MS = 90000;
  const video = document.getElementById("video");
  let recorder = null;
  let chunks = [];
  let recordedBytes = 0;
  let recordingError = null;
  let startedAtMs = null;
  let stoppedPromise = null;
  let resolveStop = null;
  let rejectStop = null;
  let stopAtMs = null;
  let onChunk = null;

  function supportedMimeType() {
    const formats = ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4;codecs=avc1,mp4a.40.2", "video/mp4"];
    return formats.find(type => MediaRecorder.isTypeSupported(type)) || "";
  }

  // Let the browser display the camera track directly. Detailed inference
  // graphics belong to the later video render, not the live capture loop.
  function startPreview() {
    if (!video.videoWidth || !video.videoHeight) throw new Error("카메라 화면을 준비할 수 없습니다.");
    video.style.opacity = "1";
  }

  function stopPreview() {
    video.style.opacity = "0";
  }

  /** Start one raw clip. The browser encodes only while this recorder is active. */
  function startRaw(chunkCallback = null, endedCallback = null, audioStream = null) {
    if (recorder) throw new Error("이미 영상 구간을 기록하고 있습니다.");
    const cameraTrack = video.srcObject?.getVideoTracks?.()[0];
    if (!window.MediaRecorder || !cameraTrack || cameraTrack.readyState !== "live") {
      throw new Error("원본 카메라 영상 기록을 시작할 수 없습니다.");
    }
    const mimeType = supportedMimeType();
    const audioTracks = audioStream?.getAudioTracks?.().filter(track => track.readyState === "live") || [];
    const current = new MediaRecorder(new MediaStream([cameraTrack, ...audioTracks]), {
      ...(mimeType ? { mimeType } : {}),
      videoBitsPerSecond: window.GConfig.get().recording.video_bits_per_second,
    });
    chunks = [];
    recordedBytes = 0;
    recordingError = null;
    onChunk = chunkCallback;
    stopAtMs = null;
    stoppedPromise = new Promise((resolve, reject) => { resolveStop = resolve; rejectStop = reject; });
    // Browser initiated MediaRecorder errors may arrive before the UI asks to stop.
    void stoppedPromise.catch(() => {});
    current.ondataavailable = event => {
      if (!event.data?.size || recordingError) return;
      if (onChunk?.(recordedBytes + event.data.size) === false) {
        recordingError = new Error("영상 기록 용량 120MB에 도달했습니다.");
        if (current.state !== "inactive") current.stop();
        return;
      }
      chunks.push(event.data);
      recordedBytes += event.data.size;
    };
    current.onerror = event => {
      recordingError = event.error || new Error("원본 카메라 영상 기록 실패");
      if (current.state !== "inactive") {
        try { current.stop(); } catch (_) { /* onstop may already be queued. */ }
      }
    };
    current.onstop = () => {
      if (recorder !== current) return;
      const endedAtMs = stopAtMs ?? Math.min(Math.max(
        Math.round(performance.timeOrigin + performance.now()), startedAtMs + 1), startedAtMs + MAX_CLIP_MS);
      const blob = new Blob(chunks, { type: current.mimeType || "video/webm" });
      const result = { blob, started_at_ms: startedAtMs, ended_at_ms: endedAtMs };
      const error = recordingError;
      recorder = null;
      chunks = [];
      recordedBytes = 0;
      startedAtMs = null;
      stopAtMs = null;
      onChunk = null;
      recordingError = null;
      current.onstop = null;
      current.ondataavailable = null;
      current.onerror = null;
      if (error) rejectStop(error);
      else resolveStop(blob.size ? result : null);
      resolveStop = rejectStop = null;
      endedCallback?.(error);
    };
    current.start(window.GConfig.get().recording.chunk_interval_ms);
    recorder = current;
    startedAtMs = Math.round(performance.timeOrigin + performance.now());
    return startedAtMs;
  }

  /** Finalize the current clip before the camera track is closed. */
  function stopRaw() {
    if (!recorder) return stoppedPromise || Promise.resolve(null);
    const current = recorder;
    if (stopAtMs === null) stopAtMs = Math.min(Math.max(
      Math.round(performance.timeOrigin + performance.now()), startedAtMs + 1), startedAtMs + MAX_CLIP_MS);
    if (current.state !== "inactive") current.stop();
    return stoppedPromise;
  }

  function active() { return recorder?.state === "recording"; }
  function bytes() { return recordedBytes; }
  function pause() { stopPreview(); }
  function resume() { startPreview(); }

  return { startPreview, stopPreview, startRaw, stopRaw, active, bytes, pause, resume };
})();
