/**
 * file_path: frontend/js/api.js
 *
 * 모바일 화면의 세션 시작·프레임 추론·녹화 업로드 요청을 묶는다.
 */
window.GApi = (() => {
  const CHUNK_BYTES = 4 * 1024 * 1024;
  let requestSequence = 0;
  const now = () => globalThis.performance?.now?.() ?? Date.now();
  const isRetryable = error => error?.retryable === true;
  const requestId = () => globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${(++requestSequence).toString(36)}-${Math.random().toString(36).slice(2)}`;
  function diagnose(type, fields) {
    try { window.GDiagnostics?.record(type, fields); } catch (_) { /* Diagnostics must not interrupt guidance. */ }
  }
  function abortError() { return Object.assign(new Error("요청을 취소했습니다."), { name: "AbortError", retryable: false }); }
  function retryDelay(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) return reject(abortError());
      const finish = () => { signal?.removeEventListener("abort", cancel); resolve(); };
      const timer = setTimeout(finish, ms);
      const cancel = () => { clearTimeout(timer); signal?.removeEventListener("abort", cancel); reject(abortError()); };
      signal?.addEventListener("abort", cancel, { once: true });
    });
  }
  async function attemptRequest(path, options, metadata, attempt) {
    const started = now(), request_id = requestId(), controller = new AbortController();
    const callerSignal = options.signal;
    let stage = "request", response, timeout, cancel;
    const details = () => ({ ...metadata, path: path.split(/[?#]/, 1)[0], method: options.method || "GET",
      attempt, request_id, elapsed_ms: Math.max(0, Math.round(now() - started)),
      http_status: response?.status, server_request_id: response?.headers?.get?.("X-Request-ID") || undefined });
    try {
      const interrupted = new Promise((_, reject) => {
        cancel = () => { controller.abort(); reject(abortError()); };
        if (callerSignal?.aborted) return cancel();
        callerSignal?.addEventListener("abort", cancel, { once: true });
        timeout = setTimeout(() => {
          stage = "timeout";
          controller.abort();
          reject(Object.assign(new Error("서버 응답 시간이 초과됐습니다."), { name: "TimeoutError" }));
        }, metadata.timeout_ms || 15000);
      });
      const operation = (async () => {
        if (callerSignal?.aborted) throw abortError();
        response = await fetch(path, { ...options, signal: controller.signal,
          headers: { ...options.headers, "X-Request-ID": request_id } });
        stage = "response_body";
        const body = await response.json();
        if (!response.ok) {
          stage = "http";
          const error = new Error(typeof body?.detail === "string" ? body.detail
            : body?.error?.message || `서버 오류 (${response.status})`);
          error.code = body?.error?.code || body?.detail?.code;
          throw error;
        }
        return body;
      })();
      const body = await Promise.race([operation, interrupted]);
      if (attempt > 1) diagnose("request_recovered", details());
      return body;
    } catch (cause) {
      const cancelled = callerSignal?.aborted;
      const timedOut = stage === "timeout";
      const message = timedOut ? "서버 응답 시간이 초과됐습니다."
        : stage === "response_body" && response && !response.ok ? `서버 오류 (${response.status}): 응답을 읽을 수 없습니다.`
        : cause?.message || "서버 요청에 실패했습니다.";
      const error = new Error(message);
      error.name = cancelled ? "AbortError" : timedOut ? "TimeoutError" : cause?.name || "Error";
      error.code = cause?.code;
      error.status = response?.status;
      error.request_id = request_id;
      error.stage = cancelled || (!timedOut && cause?.name === "AbortError") ? "abort" : stage;
      error.retryable = error.stage !== "abort" && (timedOut ||
        (error.stage === "request" && (cause?.name === "TypeError" || cause?.name === "NetworkError")) ||
        (error.stage === "response_body" && (!response || response.ok)) ||
        response?.status === 429 || response?.status >= 500);
      diagnose(error.stage === "abort" ? "request_aborted" : "request_error",
        { ...details(), stage: error.stage, error_name: error.name, error_message: error.message, retryable: error.retryable });
      throw error;
    } finally {
      clearTimeout(timeout);
      callerSignal?.removeEventListener("abort", cancel);
    }
  }
  async function request(path, options = {}, metadata = {}, retry = false) {
    for (let attempt = 1; attempt <= (retry ? 3 : 1); attempt++) {
      try { return await attemptRequest(path, options, metadata, attempt); }
      catch (error) {
        if (!retry || attempt === 3 || !isRetryable(error) || options.signal?.aborted) throw error;
        diagnose("request_retry", { ...metadata, path: path.split(/[?#]/, 1)[0], method: options.method || "GET",
          attempt, request_id: error.request_id, stage: error.stage, http_status: error.status });
        await retryDelay(attempt * 500, options.signal);
      }
    }
  }

  function config() {
    return request("/api/config", { cache: "no-store" }, { operation: "config" }, true);
  }

  // 휴대폰 모델로 테스트 세션 생성
  /** 추론 모델 로딩이 끝나면 세션 번호를 반환한다. */
  function start(deviceName, note, busHighres = false) {
    return request("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_name: deviceName, note, bus_highres: busHighres }) },
      { operation: "session_start", timeout_ms: 120000 });
  }

  // JPEG 한 장 전송
  /** 한 번에 한 장의 카메라 프레임을 전송한다. */
  function frame(sessionId, frameId, capturedAtMs, blob, signal, busBlob = null, busCapturedAtMs = null) {
    const body = new FormData();
    body.append("frame_id", String(frameId));
    body.append("captured_at_ms", String(capturedAtMs));
    body.append("image", blob, "frame.jpg");
    if (busBlob && busCapturedAtMs !== null) {
      body.append("bus_image", busBlob, "bus-frame.jpg");
      body.append("bus_captured_at_ms", String(busCapturedAtMs));
    }
    return request(`/api/sessions/${sessionId}/frames`, { method: "POST", body, signal },
      { operation: "frame", session_id: sessionId, frame_id: frameId, timeout_ms: 10000 }, true);
  }

  // WebM 녹화물 저장
  /** 녹화한 카메라와 안내 음성 파일을 서버 세션에 올린다. */
  function recording(sessionId, blob) {
    const body = new FormData();
    body.append("video", blob, "recording.webm");
    return request(`/api/sessions/${sessionId}/recording`, { method: "POST", body },
      { operation: "recording", session_id: sessionId, timeout_ms: 120000 });
  }

  // 원본 카메라 영상 저장
  /** 오버레이와 현장 소리가 없는 카메라 영상을 서버에 올린다. */
  function camera(sessionId, blob) {
    const body = new FormData();
    body.append("video", blob, "camera.webm");
    return request(`/api/sessions/${sessionId}/camera`, { method: "POST", body },
      { operation: "camera", session_id: sessionId, timeout_ms: 120000 });
  }

  // 브라우저 녹화와 업로드 실패를 세션 로그에 기록
  function recordingEvent(sessionId, kind, status, detail) {
    return request(`/api/sessions/${sessionId}/recording-events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, status, detail }),
    }, { operation: "recording_event", session_id: sessionId });
  }

  function heartbeat(sessionId) {
    return request(`/api/sessions/${sessionId}/heartbeat`, { method: "POST", cache: "no-store" },
      { operation: "heartbeat", session_id: sessionId });
  }

  function timings(sessionId, records) {
    return request(`/api/sessions/${sessionId}/timings`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records }),
    }, { operation: "timings", session_id: sessionId });
  }

  function boarding(sessionId, action, arrivalEventId, busNumber = null) {
    return request(`/api/sessions/${sessionId}/boarding`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, arrival_event_id: arrivalEventId, bus_number: busNumber }),
    }, { operation: "boarding", session_id: sessionId });
  }

  /** Upload one selected clip after live inference and the session have stopped. */
  async function uploadClip(sessionId, clip, onProgress = () => {}) {
    const base = `/api/sessions/${encodeURIComponent(sessionId)}/clips/${clip.index}`;
    const blob = clip.blob;
    if (!blob?.size) throw new Error("원본 영상이 비어 있습니다.");
    const chunkCount = Math.ceil(blob.size / CHUNK_BYTES);
    const frames = clip.frames || [];
    const total = chunkCount + frames.length + 1;
    let done = 0;
    for (let index = 0; index < chunkCount; index++) {
      const body = new FormData();
      body.append("video", blob.slice(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES, blob.type),
        `clip_${clip.index}_part_${index}`);
      await request(`${base}/chunks/${index}`, { method: "POST", body },
        { operation: "clip_chunk", session_id: sessionId, clip_id: clip.index, timeout_ms: 30000 }, true);
      onProgress(++done, total);
    }
    for (const frame of frames) {
      const body = new FormData();
      body.append("captured_at_ms", String(frame.captured_at_ms));
      body.append("image", frame.image, `frame_${frame.frame_id}.jpg`);
      if (frame.mask_png) body.append("mask_png", frame.mask_png);
      if (frame.overlay_png) body.append("overlay_png", frame.overlay_png);
      await request(`${base}/frames/${frame.frame_id}`, { method: "POST", body },
        { operation: "clip_frame", session_id: sessionId, clip_id: clip.index, frame_id: frame.frame_id, timeout_ms: 30000 }, true);
      onProgress(++done, total);
    }
    const complete = await request(`${base}/complete`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mime_type: blob.type || "video/webm", chunk_count: chunkCount,
        size_bytes: blob.size, started_at_ms: clip.started_at_ms, ended_at_ms: clip.ended_at_ms }) },
      { operation: "clip_complete", session_id: sessionId, clip_id: clip.index, timeout_ms: 130000 }, true);
    onProgress(++done, total);
    return complete;
  }

  function clips(sessionId) {
    return request(`/api/sessions/${encodeURIComponent(sessionId)}/clips`, { cache: "no-store" },
      { operation: "clip_status", session_id: sessionId }, true);
  }

  function nearbyBusArrival({ busNumber, latitude, longitude, accuracyM, signal }) {
    const query = new URLSearchParams({ bus_number: busNumber, latitude, longitude });
    if (Number.isFinite(accuracyM)) query.set("accuracy_m", accuracyM);
    return request(`/api/nearby-bus-arrival?${query}`, { signal, cache: "no-store" },
      { operation: "bus_arrival" });
  }

  function busStatus() {
    return request("/api/bus/status", { cache: "no-store" }, { operation: "bus_status" });
  }

  function busEvents(sessionId, events) {
    return request(`/api/sessions/${sessionId}/bus-events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
    }, { operation: "bus_events", session_id: sessionId });
  }

  // 테스트 종료
  /** 파일로 저장된 프레임 수를 포함한 요약을 받는다. */
  function stop(sessionId) {
    return request("/api/sessions/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) },
      { operation: "session_stop", session_id: sessionId }, true);
  }

  return { config, start, frame, recording, camera, uploadClip, clips, recordingEvent, timings, heartbeat, boarding, stop,
    nearbyBusArrival, busStatus, busEvents, isRetryable };
})();
