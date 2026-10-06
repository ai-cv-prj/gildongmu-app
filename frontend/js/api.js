/**
 * file_path: frontend/js/api.js
 *
 * 모바일 화면의 세션 시작·프레임 추론·녹화 업로드 요청을 묶는다.
 */
window.GApi = (() => {
  // JSON 응답 오류를 읽기 쉬운 문장으로 변환
  /** 서버가 반환한 오류 본문을 검사한다. */
  async function result(response) {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof body.detail === "string" ? body.detail
        : body.error?.message || `서버 오류 (${response.status})`);
      error.code = body.error?.code || body.detail?.code;
      error.status = response.status;
      throw error;
    }
    return body;
  }

  // 휴대폰 모델로 테스트 세션 생성
  /** 추론 모델 로딩이 끝나면 세션 번호를 반환한다. */
  function start(deviceName, note, busHighres = false) {
    return fetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_name: deviceName, note, bus_highres: busHighres }) }).then(result);
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
    return fetch(`/api/sessions/${sessionId}/frames`, { method: "POST", body, signal }).then(result);
  }

  // WebM 녹화물 저장
  /** 녹화한 카메라와 안내 음성 파일을 서버 세션에 올린다. */
  function recording(sessionId, blob) {
    const body = new FormData();
    body.append("video", blob, "recording.webm");
    return fetch(`/api/sessions/${sessionId}/recording`, { method: "POST", body }).then(result);
  }

  // 원본 카메라 영상 저장
  /** 오버레이와 현장 소리가 없는 카메라 영상을 서버에 올린다. */
  function camera(sessionId, blob) {
    const body = new FormData();
    body.append("video", blob, "camera.webm");
    return fetch(`/api/sessions/${sessionId}/camera`, { method: "POST", body }).then(result);
  }

  // 브라우저 녹화와 업로드 실패를 세션 로그에 기록
  function recordingEvent(sessionId, kind, status, detail) {
    return fetch(`/api/sessions/${sessionId}/recording-events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, status, detail }),
    }).then(result);
  }

  function heartbeat(sessionId) {
    return fetch(`/api/sessions/${sessionId}/heartbeat`, { method: "POST", cache: "no-store" }).then(result);
  }

  function timings(sessionId, records) {
    return fetch(`/api/sessions/${sessionId}/timings`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records }),
    }).then(result);
  }

  function boarding(sessionId, action, arrivalEventId, busNumber = null) {
    return fetch(`/api/sessions/${sessionId}/boarding`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, arrival_event_id: arrivalEventId, bus_number: busNumber }),
    }).then(result);
  }

  const CHUNK_BYTES = 4 * 1024 * 1024;
  const retryDelay = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function retryUpload(path, options) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await result(await fetch(path, options));
      } catch (error) {
        if (attempt === 2 || (error.status && error.status < 500 && error.status !== 429)) throw error;
        await retryDelay(500 * (attempt + 1));
      }
    }
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
      await retryUpload(`${base}/chunks/${index}`, { method: "POST", body });
      onProgress(++done, total);
    }
    for (const frame of frames) {
      const body = new FormData();
      body.append("captured_at_ms", String(frame.captured_at_ms));
      body.append("image", frame.image, `frame_${frame.frame_id}.jpg`);
      if (frame.mask_png) body.append("mask_png", frame.mask_png);
      if (frame.overlay_png) body.append("overlay_png", frame.overlay_png);
      await retryUpload(`${base}/frames/${frame.frame_id}`, { method: "POST", body });
      onProgress(++done, total);
    }
    const complete = await retryUpload(`${base}/complete`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mime_type: blob.type || "video/webm", chunk_count: chunkCount,
        size_bytes: blob.size, started_at_ms: clip.started_at_ms, ended_at_ms: clip.ended_at_ms }) });
    onProgress(++done, total);
    return complete;
  }

  function clips(sessionId) {
    return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/clips`, { cache: "no-store" }).then(result);
  }

  function nearbyBusArrival({ busNumber, latitude, longitude, accuracyM, signal }) {
    const query = new URLSearchParams({ bus_number: busNumber, latitude, longitude });
    if (Number.isFinite(accuracyM)) query.set("accuracy_m", accuracyM);
    return fetch(`/api/nearby-bus-arrival?${query}`, { signal, cache: "no-store" }).then(result);
  }

  function busStatus() {
    return fetch("/api/bus/status", { cache: "no-store" }).then(result);
  }

  function busEvents(sessionId, events) {
    return fetch(`/api/sessions/${sessionId}/bus-events`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
    }).then(result);
  }

  // 테스트 종료
  /** 파일로 저장된 프레임 수를 포함한 요약을 받는다. */
  function stop(sessionId) {
    return fetch("/api/sessions/stop", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }) }).then(result);
  }

  return { start, frame, recording, camera, uploadClip, clips, recordingEvent, timings, heartbeat, boarding, stop,
    nearbyBusArrival, busStatus, busEvents };
})();
