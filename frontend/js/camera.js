/**
 * file_path: frontend/js/camera.js
 *
 * 후면 카메라에서 전송용 JPEG를 만들고 가능하면 Worker로 인코딩한다.
 */
window.GCamera = (() => {
  const video = document.getElementById("video");
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d", { willReadFrequently: false });
  let stream = null;
  let onEnded = null;
  let encoder = null, encoderFailed = false, pendingCapture = null;
  let videoFrameFailed = false;
  let captureId = 0, cameraVersion = 0, lastBackend = "canvas";
  let capturing = false;

  // 인코더 종료 및 대기 요청 해제
  /** Worker를 종료하고 진행 중인 캡처가 영원히 기다리지 않도록 한다. */
  function closeEncoder(error) {
    encoder?.terminate();
    encoder = null;
    const pending = pendingCapture;
    pendingCapture = null;
    if (pending) { clearTimeout(pending.timer); pending.reject(error); }
  }

  // 캡처 작업용 Worker 준비
  /** 필요한 API가 없거나 Worker가 실패한 경우 기존 canvas 경로를 사용한다. */
  function getEncoder() {
    if (encoderFailed || !window.Worker || !window.OffscreenCanvas
        || (!(window.VideoFrame && !videoFrameFailed) && !window.createImageBitmap)) return null;
    if (encoder) return encoder;
    try {
      const current = new Worker("/static/js/capture-worker.js?v=latency-v3");
      encoder = current;
      current.onmessage = ({ data }) => {
        const pending = pendingCapture;
        if (encoder !== current || !pending || data.id !== pending.id) return;
        pendingCapture = null;
        clearTimeout(pending.timer);
        if (data.error || !data.blob) pending.reject(new Error(data.error || "JPEG 생성 실패"));
        else pending.resolve(data.blob);
      };
      current.onerror = current.onmessageerror = () => {
        if (encoder !== current) return;
        encoderFailed = true;
        closeEncoder(new Error("캡처 Worker 실행 실패"));
      };
      return current;
    } catch (_) {
      encoderFailed = true;
      return null;
    }
  }

  // VideoFrame/ImageBitmap 소유권을 Worker에 넘겨 JPEG 생성
  /** 대기열 없이 한 장만 처리하고 2초 내 완료되지 않으면 기존 경로로 전환한다. */
  function encodeFrame(worker, bitmap, width, height, quality) {
    return new Promise((resolve, reject) => {
      const id = ++captureId;
      const timer = setTimeout(() => {
        encoderFailed = true;
        closeEncoder(new Error("캡처 Worker 시간 초과"));
      }, 2000);
      pendingCapture = { id, resolve, reject, timer };
      try { worker.postMessage({ id, bitmap, width, height, quality }, [bitmap]); }
      catch (error) { closeEncoder(error); }
    });
  }

  // 후면 카메라 및 인코더 준비
  /** 카메라 권한을 받고 영상이 준비되면 Worker 로딩도 시작한다. */
  async function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("이 브라우저는 카메라를 지원하지 않습니다. HTTPS 주소로 접속했는지 확인하세요.");
    }
    stop();
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
    } catch (e) {
      if (e.name === "NotAllowedError") throw new Error("카메라 권한이 거부되었습니다. 브라우저 설정에서 허용해주세요.");
      if (e.name === "NotFoundError" || e.name === "OverconstrainedError") throw new Error("후면 카메라를 찾지 못했습니다.");
      throw new Error(`카메라를 열 수 없습니다: ${e.message || e.name}`);
    }
    const track = stream.getVideoTracks()[0];
    track.addEventListener("ended", () => { if (onEnded) onEnded(); });
    video.srcObject = stream;
    await new Promise((resolve) => {
      if (video.readyState >= 2) return resolve();
      video.onloadedmetadata = () => resolve();
    });
    try { await video.play(); } catch (_) { /* 자동 재생 정책 */ }
    getEncoder(); // 테스트 시작 전에 Worker 로딩을 시작한다.
    return { width: video.videoWidth, height: video.videoHeight, label: track.label };
  }

  // 카메라와 인코더 종료
  /** 진행 중인 캡처를 무효화하고 카메라 트랙과 Worker 자원을 해제한다. */
  function stop() {
    cameraVersion += 1;
    closeEncoder(new Error("카메라 종료"));
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    video.srcObject = null;
  }

  function active() {
    return !!stream && stream.getVideoTracks().some((t) => t.readyState === "live") && video.videoWidth > 0;
  }

  // 실제 프레임 캡처 및 JPEG 생성
  /** VideoFrame을 우선 전달하고 미지원·오류 시 기존 경로로 전환하며 종료 결과는 버린다. */
  async function captureFrame(maxSide, quality) {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) return null;
    const version = cameraVersion;
    const scale = Math.min(1, maxSide / Math.max(vw, vh));
    const w = Math.round(vw * scale), h = Math.round(vh * scale);
    const worker = getEncoder();
    if (worker) {
      let bitmap;
      let source = "worker";
      try {
        // 지원 브라우저에서는 메인 스레드의 비트맵 변환 대기를 생략한다.
        if (window.VideoFrame && !videoFrameFailed) {
          try {
            bitmap = new VideoFrame(video, { timestamp: Math.round(performance.now() * 1000) });
            source = "worker_video_frame";
          } catch (_) { videoFrameFailed = true; }
        }
        if (!bitmap) bitmap = await createImageBitmap(video);
        if (version !== cameraVersion) { bitmap.close(); return null; }
        if (worker !== encoder) throw new Error("캡처 Worker가 종료되었습니다");
        const blob = await encodeFrame(worker, bitmap, w, h, quality);
        lastBackend = source;
        return blob;
      } catch (error) {
        bitmap?.close();
        if (version !== cameraVersion) return null;
        encoderFailed = true;
        closeEncoder(error);
        console.warn("JPEG 생성은 기존 canvas 경로로 전환합니다.", error);
      }
    }
    lastBackend = "canvas";
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.drawImage(video, 0, 0, w, h);
    return new Promise((resolve) => canvas.toBlob((b) => resolve(b), "image/jpeg", quality));
  }

  // 대기열 없는 단일 프레임 캡처
  /** 비트맵 생성 단계부터 JPEG 완료까지 중복 캡처를 허용하지 않는다. */
  async function capture(maxSide, quality) {
    if (capturing) throw new Error("이전 캡처가 아직 처리 중입니다");
    capturing = true;
    try { return await captureFrame(maxSide, quality); }
    finally { capturing = false; }
  }

  // 실제 캡처 경로 조회
  /** VideoFrame Worker, ImageBitmap Worker, 기존 canvas 중 실제 사용 경로를 반환한다. */
  function captureBackend() { return lastBackend; }

  return { start, stop, active, capture, video, captureBackend, setOnEnded: (fn) => { onEnded = fn; } };
})();
