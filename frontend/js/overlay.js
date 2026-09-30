/**
 * file_path: frontend/js/overlay.js
 *
 * 같은 카메라 화면에 보행 마스크, 예상보행경로, 위험 객체, 신호등을 겹쳐 그린다.
 */
window.GOverlay = (() => {
  const canvas = document.getElementById("overlay");
  const ctx = canvas.getContext("2d");
  let version = 0;

  // 프레임 크기에 맞는 캔버스 준비
  /** 카메라와 같은 비율로 오버레이 좌표를 맞춘다. */
  function size(width, height) {
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }

  // 정규화된 다각형 표시
  /** 예상보행경로의 좌우 이동을 그대로 화면에 표시한다. */
  function polygon(points, stroke, fill) {
    if (!Array.isArray(points) || points.length < 3) return;
    ctx.beginPath();
    points.forEach(([x, y], index) => index ? ctx.lineTo(x * canvas.width, y * canvas.height)
      : ctx.moveTo(x * canvas.width, y * canvas.height));
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.strokeStyle = stroke;
    ctx.lineWidth = Math.max(2, canvas.width / 320);
    ctx.stroke();
  }

  // 정규화된 탐지 박스와 라벨 표시
  /** 보행 장애물과 신호등을 색상과 짧은 이름으로 구분한다. */
  function boxes(items, kind) {
    for (const item of items || []) {
      const [x1, y1, x2, y2] = item.xyxy || [];
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
      const selected = item.selection_status === "selected";
      const level = item.alert_level || item.risk_level;
      const color = kind === "crosswalk" ? "#d965cc" : kind === "traffic"
        ? selected ? item.signal_state === "green" ? "#38db77" : item.signal_state === "red" ? "#ff6172" : "#f5d66d" : "#69b0ff"
        : level === "danger" ? "#ff6571" : level === "caution" ? "#ffb766" : "#b4b4b4";
      ctx.strokeStyle = color;
      ctx.lineWidth = selected || (kind === "walking" && ["caution", "danger"].includes(level)) ? 4 : 2;
      const x = x1 * canvas.width, y = y1 * canvas.height;
      const w = (x2 - x1) * canvas.width, h = (y2 - y1) * canvas.height;
      ctx.strokeRect(x, y, w, h);
      const name = kind === "crosswalk" ? "횡단보도" : kind === "traffic"
        ? selected ? `신호 ${item.signal_state || "확인 중"}` : "신호 후보"
        : item.display_label || item.class_name || "장애물";
      ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
      const textWidth = ctx.measureText(name).width + 12;
      const labelY = Math.max(2, y - 26);
      ctx.fillStyle = color;
      ctx.fillRect(x, labelY, textWidth, 24);
      ctx.fillStyle = "#08131f";
      ctx.fillText(name, x + 6, labelY + 17);
    }
  }

  // 서버 응답의 PNG 마스크와 위험·신호 결과 합성
  /** 새 결과가 도착했을 때 이전 프레임의 비동기 이미지 로딩을 무효화한다. */
  function render(result) {
    const current = ++version;
    size(result.image_width, result.image_height);
    const draw = mask => {
      if (current !== version) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (mask) ctx.drawImage(mask, 0, 0, canvas.width, canvas.height);
      const roi = result.walking?.event?.roi || {};
      for (const points of roi.corridor_polygons || [roi.corridor_polygon])
        polygon(points, "#4ce3fa", "#4ce3fa20");
      polygon(roi.immediate_polygon, "#ff88ba", "#ff88ba24");
      boxes(result.walking?.detections, "walking");
      boxes(result.traffic?.crosswalks, "crosswalk");
      boxes(result.traffic?.detections, "traffic");
    };
    if (!result.walking?.mask_png) return draw(null);
    const mask = new Image();
    mask.onload = () => draw(mask);
    mask.onerror = () => draw(null);
    mask.src = `data:image/png;base64,${result.walking.mask_png}`;
  }

  // 종료할 때 이전 추론 화면 제거
  /** 카메라 영상만 남기고 분석 결과를 지운다. */
  function clear() {
    version++;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  return { size, render, clear };
})();
