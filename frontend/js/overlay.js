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
      const baseName = kind === "crosswalk" ? "횡단보도" : kind === "traffic"
        ? selected ? `신호 ${item.signal_state || "확인 중"}` : "신호 후보"
        : item.display_label || item.class_name || "장애물";
      const identifiers = kind === "walking" ? [
        Number.isInteger(item.track_id) ? `T${item.track_id}` : null,
        Number.isInteger(item.event_id) ? `E${item.event_id}` : null,
      ].filter(Boolean) : [];
      const name = identifiers.length ? `${baseName} | ${identifiers.join(" · ")}` : baseName;
      ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
      const textWidth = ctx.measureText(name).width + 12;
      const labelY = Math.max(2, y - 26);
      ctx.fillStyle = color;
      ctx.fillRect(x, labelY, textWidth, 24);
      ctx.fillStyle = "#08131f";
      ctx.fillText(name, x + 6, labelY + 17);
    }
  }

  // 현장 테스트용 정류장 판정. 캔버스에 그리므로 오버레이 영상에도 남는다.
  function stopDiagnostic(event) {
    const state = event?.status || "unavailable";
    const basis = { left: "좌측", right: "우측", bottom: "하단" }[event?.basis];
    const position = basis ? `${basis} ` : "";
    const color = state === "nearby" ? "#57d7ab"
      : state === "candidate" ? "#ffce73" : "#c2ceda";
    const label = state === "nearby" ? `${position}정류장 ${event.held ? "근접 유지 · 재확인 중" : "근접 추정"}`
      : event?.arrival_recorded ? "정류장 확인 기록 · 재확인 중"
      : state === "candidate" ? event.held ? `${position}정류장 후보 유지`
        : `${position}정류장 후보 ${event.observations}/${event.required_observations}`
      : state === "not_detected" ? "정류장 미검출" : "정류장 판정 보류";
    const box = event?.xyxy;
    ctx.save();
    if (!event?.held && ["nearby", "candidate"].includes(state) && Array.isArray(box)
        && box.length === 4 && box.every(Number.isFinite)) {
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(4, canvas.width / 120);
      ctx.strokeRect(box[0] * canvas.width, box[1] * canvas.height,
        (box[2] - box[0]) * canvas.width, (box[3] - box[1]) * canvas.height);
    }
    ctx.font = `bold ${Math.max(14, Math.min(20, canvas.width / 32))}px system-ui`;
    const bannerWidth = Math.min(canvas.width - 16, ctx.measureText(label).width + 22);
    const x = canvas.width - bannerWidth - 8;
    const y = 52;
    ctx.fillStyle = "#08131feb";
    ctx.fillRect(x, y, bannerWidth, 36);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, bannerWidth, 36);
    ctx.fillStyle = color;
    ctx.fillText(label, x + 11, y + 24);
    ctx.restore();
  }

  // 횡단보도 안전 경계 표시
  /** 가상 사용자 위치 높이에서 추정한 좌우 경계와 현재 발 위치를 그린다. */
  function crosswalkSafety(event) {
    const geometry = event?.geometry;
    const values = [geometry?.left_x, geometry?.right_x, geometry?.foot_x, geometry?.foot_y];
    if (!values.every(Number.isFinite)) return;
    const [left, right, foot, y] = values;
    const color = String(event.status).startsWith("outside") ? "#ff435b" : "#f16be0";
    ctx.strokeStyle = color;
    ctx.lineWidth = 5;
    ctx.beginPath();
    ctx.moveTo(left * canvas.width, y * canvas.height);
    ctx.lineTo(right * canvas.width, y * canvas.height);
    ctx.stroke();
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.arc(foot * canvas.width, y * canvas.height, 7, 0, Math.PI * 2);
    ctx.fill();
  }

  // 현재 횡단보도 상태 표시
  /** 모바일 화면 오른쪽 위에 현재 횡단 상태를 짧은 배지로 표시한다. */
  function crosswalkStatus(event) {
    const status = event?.status;
    if (!status || status === "disabled") return;
    const text = `CROSSWALK: ${status}`;
    ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
    const padding = 8;
    const height = Math.max(26, canvas.height / 24);
    const width = ctx.measureText(text).width + padding * 2;
    const x = Math.max(0, canvas.width - width - 8);
    const y = 8;
    ctx.fillStyle = "rgba(24, 24, 24, 0.88)";
    ctx.fillRect(x, y, width, height);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(text, x + padding, y + height - 8);
  }

  // 현재 장애물 이동 행동 표시
  /** 모바일 화면 왼쪽 위에 마지막으로 판단한 이동 행동을 표시한다. */
  function actionStatus(event) {
    const action = event?.last_action ?? "none";
    const text = `ACTION: ${action}`;
    ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
    const padding = 8;
    const height = Math.max(26, canvas.height / 24);
    const width = ctx.measureText(text).width + padding * 2;
    const x = 8;
    const y = 8;
    ctx.fillStyle = "rgba(24, 24, 24, 0.88)";
    ctx.fillRect(x, y, width, height);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(text, x + padding, y + height - 8);
  }

  // 하단 횡단보도 판단 ROI 표시
  /** 서버 판정에 사용한 고정 ROI를 보라색 테두리로 표시한다. */
  function crosswalkRoi(event) {
    const roi = event?.crosswalk_roi;
    const values = [roi?.left, roi?.right, roi?.top, roi?.bottom];
    if (!values.every(Number.isFinite)) return;
    const [left, right, top, bottom] = values;
    polygon([[left, top], [right, top], [right, bottom], [left, bottom]],
      "#9b5de5", "#9b5de50d");
  }

  // 보행로 판정용 가상 발 ROI 표시
  /** 흰색 가상 발 주변에서 실제 판정에 사용한 작은 영역을 녹색 테두리로 표시한다. */
  function walkingSurfaceRoi(event) {
    const roi = event?.roi;
    const values = [roi?.left, roi?.right, roi?.top, roi?.bottom];
    if (!values.every(Number.isFinite)) return;
    const [left, right, top, bottom] = values;
    ctx.strokeStyle = "#50e65a";
    ctx.lineWidth = Math.max(2, canvas.width / 320);
    ctx.strokeRect(left * canvas.width, top * canvas.height,
      (right - left) * canvas.width, (bottom - top) * canvas.height);
  }

  // 현재 보행로 상태 표시
  /** 횡단보도 상태 아래에 독립적인 보행로 이탈 상태를 표시한다. */
  function walkingSurfaceStatus(event) {
    const status = event?.status;
    if (!status || status === "disabled") return;
    const text = `WALKWAY: ${status}`;
    ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
    const padding = 8;
    const height = Math.max(26, canvas.height / 24);
    const width = ctx.measureText(text).width + padding * 2;
    const x = Math.max(0, canvas.width - width - 8);
    const y = 8 + height + 8;
    ctx.fillStyle = "rgba(24, 24, 24, 0.88)";
    ctx.fillRect(x, y, width, height);
    ctx.fillStyle = "#ffffff";
    ctx.fillText(text, x + padding, y + height - 8);
  }

  // 서버 응답의 PNG 마스크와 위험·신호 결과 합성
  /** 새 결과가 도착했을 때 이전 프레임의 비동기 이미지 로딩을 무효화한다. */
  function render(result, onDrawn = () => {}) {
    const current = ++version;
    size(result.image_width, result.image_height);
    const draw = mask => {
      if (current !== version) return onDrawn("superseded");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (Number.isFinite(result.captured_at_ms) && typeof performance !== "undefined"
          && Number.isFinite(performance.timeOrigin)
          && performance.timeOrigin + performance.now() - result.captured_at_ms > 400) {
        return onDrawn("stale");
      }
      if (mask) ctx.drawImage(mask, 0, 0, canvas.width, canvas.height);
      const roi = result.walking?.event?.roi || {};
      for (const points of roi.corridor_polygons || [roi.corridor_polygon])
        polygon(points, "#4ce3fa", "#4ce3fa20");
      polygon(roi.immediate_polygon, "#ff88ba", "#ff88ba24");
      crosswalkRoi(result.crosswalk?.event);
      walkingSurfaceRoi(result.walking_surface?.event);
      boxes(result.walking?.detections, "walking");
      boxes(result.traffic?.detections, "traffic");
      crosswalkSafety(result.crosswalk?.event);
      actionStatus(result.walking?.event);
      crosswalkStatus(result.crosswalk?.event);
      walkingSurfaceStatus(result.walking_surface?.event);
      stopDiagnostic(result.stop_proximity);
      onDrawn("drawn");
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
