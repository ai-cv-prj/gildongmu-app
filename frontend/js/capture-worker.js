/**
 * file_path: frontend/js/capture-worker.js
 *
 * 카메라 프레임을 별도 스레드에서 축소하고 JPEG로 인코딩한다.
 */
let canvas = null;
let ctx = null;

// 전송용 JPEG 생성
/** 받은 프레임을 지정된 크기와 품질로 변환해 호출자에게 돌려준다. */
self.onmessage = async ({ data }) => {
  const { id, bitmap, width, height, quality } = data;
  try {
    if (!canvas) {
      canvas = new OffscreenCanvas(width, height);
      ctx = canvas.getContext("2d", { alpha: false });
    }
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    ctx.drawImage(bitmap, 0, 0, width, height);
    const blob = await canvas.convertToBlob({ type: "image/jpeg", quality });
    self.postMessage({ id, blob });
  } catch (error) {
    self.postMessage({ id, error: error.message || "JPEG 생성 실패" });
  } finally {
    bitmap.close();
  }
};
