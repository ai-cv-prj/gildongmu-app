/**
 * 같은 카메라 화면에 보행 마스크, 예상보행경로, 위험 객체, 신호등을 겹쳐 그린다.
 */
window.GOverlay = (() => {
  const canvas = document.getElementById("overlay");
  const ctx = canvas.getContext("2d");
  let version = 0;
  const BUS_COLORS = Object.freeze({ mint: "#21d7bb", checkingBlue: "#7cbdff",
    otherRed: "#ff6172", muted: "#b9c5d5" });
  const BUS_BOX_MAX_AGE_MS = 1000;

  // OCR 점수만 표시한다. 버스 검출 confidence는 번호 판독 점수와 다르다.
  function ocrConfidence(score) {
    return Number.isFinite(score) && score >= 0 && score <= 1
      ? `OCR ${(score * 100).toFixed(1)}%` : "";
  }

  /** 카메라와 같은 비율로 오버레이 좌표를 맞춘다. */
  function size(width, height) {
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }

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

  /** 음성 판단과 같은 30%·70% 경계를 화면 전체 높이의 점선으로 표시한다. */
  function directionBoundaries() {
    const guidance = window.GConfig?.get?.().audio?.guidance || {};
    const boundaries = [guidance.walking_left_max_ratio ?? .30,
      guidance.walking_right_min_ratio ?? .70];
    ctx.save();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.65)";
    ctx.lineWidth = Math.max(1, canvas.width / 480);
    ctx.setLineDash([Math.max(8, canvas.height / 45), Math.max(6, canvas.height / 60)]);
    for (const ratio of boundaries) {
      ctx.beginPath();
      ctx.moveTo(ratio * canvas.width, 0);
      ctx.lineTo(ratio * canvas.width, canvas.height);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.restore();
  }

  /** 보행 장애물과 신호등을 색상과 짧은 이름으로 구분한다. */
  function boxes(items, kind) {
    for (const item of items || []) {
      const box = item.box;
      const [x1, y1, x2, y2] = item.xyxy || (box ? [box.x1, box.y1, box.x2, box.y2] : []);
      if (![x1, y1, x2, y2].every(Number.isFinite)) continue;
      const selected = item.selection_status === "selected";
      const level = item.alert_level || item.risk_level;
      const color = kind === "bus" ? BUS_COLORS.checkingBlue : kind === "crosswalk" ? "#d965cc" : kind === "traffic"
        ? selected ? item.signal_state === "green" ? "#38db77" : item.signal_state === "red" ? "#ff6172" : "#f5d66d" : "#69b0ff"
        : level === "danger" ? "#ff6571" : level === "caution" ? "#ffb766" : "#b4b4b4";
      ctx.strokeStyle = color;
      ctx.lineWidth = selected || (kind === "walking" && ["caution", "danger"].includes(level)) ? 4 : 2;
      const x = x1 * canvas.width, y = y1 * canvas.height;
      const w = (x2 - x1) * canvas.width, h = (y2 - y1) * canvas.height;
      ctx.strokeRect(x, y, w, h);
      const baseName = kind === "bus" ? (item.extra?.text || item.extra?.route_number || "버스")
        : kind === "crosswalk" ? "횡단보도" : kind === "traffic"
        ? selected ? `신호 ${item.signal_state || "확인 중"}` : "신호 후보"
        : item.display_label || item.class_name || "장애물";
      const identifiers = kind === "walking" ? [
        Number.isInteger(item.track_id) ? `T${item.track_id}` : null,
        Number.isInteger(item.event_id) ? `E${item.event_id}` : null,
      ].filter(Boolean) : [];
      const confidence = kind === "bus" && (item.class_name === "route_number" || item.class_id === 1)
        ? ocrConfidence(item.extra?.token_score ?? item.confidence) : "";
      const name = confidence ? `${baseName} · ${confidence}`
        : identifiers.length ? `${baseName} | ${identifiers.join(" · ")}` : baseName;
      ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
      const textWidth = ctx.measureText(name).width + 12;
      const labelY = Math.max(2, y - 26);
      ctx.fillStyle = color;
      ctx.fillRect(x, labelY, textWidth, 24);
      ctx.fillStyle = "#08131f";
      ctx.fillText(name, x + 6, labelY + 17);
    }
  }

  // 버스 모드에서는 화면과 저장 영상에 같은 인식 상태와 큰 노선 번호를 표시한다.
  // mode "full": large card and labels, true/"boxes": outlines only,
  // "slim": live bus screen (existing card, compact labels, bottom step line; preview v5).
  function busScene(result, age, mode = "full") {
    const boxesOnly = mode === true || mode === "boxes";
    const slim = mode === "slim";
    const k = canvas.width / 720; // v5 layout values were drawn on a 720 px wide canvas
    const source = result.bus || {};
    const target = String(result.target_route || source.event?.target_route || "");
    // A response captured before a route change cannot label the new search.
    const bus = source.event?.target_route && String(source.event.target_route) !== target ? {} : source;
    const event = bus.event || {};
    const failed = ["error", "unavailable", "disabled"].includes(bus.status);
    const preparing = ["loading", "idle"].includes(bus.status);
    const fresh = Number.isFinite(age) && age >= 0 && age <= 3000;
    const isTarget = item => item.is_target !== false && String(item.route_number) === target;
    const visible = Array.isArray(event.buses)
      ? new Map(event.buses.map(item => [item.track_id, item])) : null;
    const evidence = !failed && !preparing && fresh
      ? [...(event.recognized_routes || []), ...(event.matches || [])]
        .filter(item => item.route_number && (item.state === "matched_candidate"
          || isTarget(item) && item.state === "recognized_single"))
        .filter(item => !visible || visible.has(item.track_id)
          && (!isTarget(item) || !visible.get(item.track_id).decision
            || visible.get(item.track_id).decision.state === item.state))
      : [];
    evidence.sort((left, right) => Number(isTarget(right)) - Number(isTarget(left))
      || Number(right.state === "matched_candidate") - Number(left.state === "matched_candidate")
      || (Number(right.token_score) || 0) - (Number(left.token_score) || 0));
    // Card and boxes use the same current evidence, independent of speech retention.
    const shown = evidence[0];
    const scale = canvas.width / 360;
    // Search is gray, reading is blue, target evidence is mint, and other routes are red.
    const { mint, checkingBlue, otherRed, muted } = BUS_COLORS;
    const colorFor = item => isTarget(item) ? mint : otherRed;
    const checking = !failed && !preparing && fresh &&
      ((bus.detections || []).some(item => item.class_name === "bus" || item.class_id === 0)
        || event.buses?.length > 0);
    const rounded = (x, y, width, height, radius, fill, stroke) => {
      ctx.fillStyle = fill;
      ctx.strokeStyle = stroke;
      if (typeof ctx.roundRect === "function") {
        ctx.beginPath();
        ctx.roundRect(x, y, width, height, radius);
        if (fill) ctx.fill();
        if (stroke) ctx.stroke();
      } else {
        if (fill) ctx.fillRect(x, y, width, height);
        if (stroke) ctx.strokeRect(x, y, width, height);
      }
    };
    const fittedText = (text, x, y, fontSize, maxWidth, color) => {
      ctx.font = `bold ${fontSize}px system-ui`;
      while (ctx.measureText(text).width > maxWidth && fontSize > 8 * scale) {
        fontSize -= scale;
        ctx.font = `bold ${fontSize}px system-ui`;
      }
      ctx.fillStyle = color;
      ctx.fillText(text, x, y);
    };
    ctx.save();
    // A bus can move substantially during OCR: geometry expires sooner than the text evidence.
    if (!failed && !preparing && age >= 0 && age <= BUS_BOX_MAX_AGE_MS) {
      // Mark an older observation without predicting the vehicle's new position.
      ctx.setLineDash?.(age > 400 ? [8 * scale, 5 * scale] : []);
      const detections = bus.detections || [];
      for (const item of detections) {
        const box = item.box;
        const bounds = item.xyxy || (box ? [box.x1, box.y1, box.x2, box.y2] : []);
        if (bounds.length !== 4 || !bounds.every(Number.isFinite)) continue;
        const [x1, y1, x2, y2] = bounds.map(value => Math.max(0, Math.min(1, value)));
        if (x2 <= x1 || y2 <= y1) continue;
        const x = x1 * canvas.width, y = y1 * canvas.height;
        const width = (x2 - x1) * canvas.width, height = (y2 - y1) * canvas.height;
        const isNumber = item.class_name === "route_number" || item.class_id === 1;
        const match = evidence.find(entry => entry.track_id != null && entry.track_id === item.track_id);
        const color = match ? colorFor(match) : checkingBlue;
        ctx.strokeStyle = isNumber ? "#ffffff" : color;
        ctx.lineWidth = slim ? (isNumber ? 2 : 3) * k : (isNumber ? 1.5 : 3) * scale;
        rounded(x, y, width, height, slim ? (isNumber ? 1 : 5) * k : (isNumber ? 2 : 6) * scale,
          null, ctx.strokeStyle);
        // OCR crops remain visible, but only validated evidence labels the whole bus with a number.
        if (isNumber) {
          if (boxesOnly) continue;
          if (slim) {
            ctx.font = `600 ${18 * k}px system-ui`;
            const tagWidth = ctx.measureText("번호 영역").width + 12 * k;
            const tagX = Math.min(x + width + 5 * k, canvas.width - tagWidth - 4 * k);
            rounded(tagX, y, tagWidth, 27 * k, 3 * k, "rgba(7, 18, 29, 0.85)", null);
            ctx.fillStyle = "#ffffff";
            ctx.fillText("번호 영역", tagX + 6 * k, y + 20 * k);
            continue;
          }
          const confidence = ocrConfidence(item.extra?.token_score ?? item.confidence);
          if (confidence) {
            ctx.font = `bold ${12 * scale}px system-ui`;
            const labelWidth = Math.min(canvas.width - 16 * scale,
              ctx.measureText(confidence).width + 12 * scale);
            const labelHeight = 22 * scale;
            const labelX = Math.max(8 * scale, Math.min(x, canvas.width - labelWidth - 8 * scale));
            // Below the crop and below the top-clamped vehicle label so its score stays visible.
            const labelY = Math.min(canvas.height - labelHeight - 8 * scale,
              Math.max(182 * scale, y + height + 3 * scale));
            rounded(labelX, labelY, labelWidth, labelHeight, 3 * scale, "#ffffff", null);
            fittedText(confidence, labelX + 6 * scale, labelY + 15 * scale,
              12 * scale, labelWidth - 12 * scale, "#08131f");
          }
          continue;
        }
        const confidence = ocrConfidence(match?.token_score);
        if (slim) {
          // Only validated evidence labels a bus; an unread bus keeps its blue outline.
          if (!match) continue;
          const label = `${match.route_number}${confidence ? ` · ${confidence}` : ""}`;
          ctx.font = `700 ${23 * k}px system-ui`;
          const labelWidth = Math.min(canvas.width - 8 * k, ctx.measureText(label).width + 22 * k);
          const labelX = Math.max(4 * k, Math.min(x, canvas.width - labelWidth - 4 * k));
          const labelY = Math.max(4 * k, y - 34 * k);
          rounded(labelX, labelY, labelWidth, 31 * k, 4 * k, color, null);
          fittedText(label, labelX + 10 * k, labelY + 23 * k, 23 * k, labelWidth - 20 * k, "#082923");
          continue;
        }
        const label = boxesOnly ? match ? `${match.route_number}번` : "버스"
          : match ? `${match.route_number}${confidence ? ` · ${confidence}` : ""}`
            : "버스 번호 확인 중";
        const fontSize = boxesOnly ? 15 : match ? 32 : 15;
        ctx.font = `bold ${fontSize * scale}px system-ui`;
        const labelWidth = Math.min(canvas.width - 16 * scale, ctx.measureText(label).width + 16 * scale);
        const labelHeight = (boxesOnly ? 25 : match ? 44 : 27) * scale;
        const labelX = Math.max(8 * scale, Math.min(x, canvas.width - labelWidth - 8 * scale));
        const labelY = Math.min(canvas.height - labelHeight - 8 * scale,
          Math.max((boxesOnly ? 8 : 130) * scale, y - labelHeight - 5 * scale));
        rounded(labelX, labelY, labelWidth, labelHeight, 6 * scale, color, null);
        fittedText(label, labelX + 8 * scale, labelY + labelHeight - 8 * scale,
          fontSize * scale, labelWidth - 16 * scale, "#071e23");
      }
    }
    ctx.setLineDash?.([]);
    if (boxesOnly) { ctx.restore(); return; }
    const x = 12 * scale, y = 12 * scale, width = canvas.width - 24 * scale;
    const height = 108 * scale, padding = 12 * scale;
    const accent = shown ? colorFor(shown) : checking ? checkingBlue : muted;
    ctx.lineWidth = 1.5 * scale;
    rounded(x, y, width, height, 12 * scale, "rgba(9, 20, 36, 0.94)", accent);
    // Keep one status line; tentative readings must not claim target confirmation.
    const confirmedTarget = shown && isTarget(shown) && shown.state === "matched_candidate";
    const title = shown && !isTarget(shown) ? `${shown.route_number} 다른 버스 확인`
      : confirmedTarget ? `${shown.route_number}번 버스 확인`
      : shown ? `${shown.route_number}번 재확인 중`
      : failed || event.errors?.length && !shown ? "번호 인식 오류"
      : preparing ? "번호 인식 준비 중"
      : checking ? "버스 번호 확인중"
      : "버스 찾는 중";
    fittedText(title, x + padding, y + 65 * scale, 26 * scale,
      width - padding * 2, accent);
    if (slim && checking) {
      // Bottom step line: what the current frame has completed and what remains.
      const buses = event.buses || [];
      const shownBus = buses.find(item => shown && item.track_id === shown.track_id) || buses[0];
      const reading = shown?.route_number
        ?? (shownBus?.observations || []).find(item => item.eligible)?.text;
      const regions = shownBus?.candidate_count ?? (bus.detections || [])
        .filter(item => item.class_name === "route_number" || item.class_id === 1).length;
      const steps = [`버스 ${buses.length || 1}대`, `번호 영역 ${regions}개`,
        ...(reading ? [`OCR ${reading}`] : [])].join("  →  ");
      const status = confirmedTarget ? "→ 목표 버스 확인" : shown && !isTarget(shown) ? "→ 다른 버스"
        : reading ? "→ 추가 확인 중" : "→ 번호 확인 중";
      const statusColor = confirmedTarget ? mint : shown && !isTarget(shown) ? otherRed : checkingBlue;
      const barX = 12 * k, barY = canvas.height - 56 * k, barWidth = canvas.width - 24 * k;
      rounded(barX, barY, barWidth, 42 * k, 6 * k, "rgba(7, 18, 29, 0.86)", null);
      ctx.font = `600 ${21 * k}px system-ui`;
      const statusWidth = ctx.measureText(status).width;
      const stepsWidth = barWidth - statusWidth - 38 * k;
      fittedText(steps, barX + 13 * k, barY + 29 * k, 21 * k, stepsWidth, "#ffffff");
      fittedText(status, barX + barWidth - statusWidth - 13 * k, barY + 29 * k, 21 * k,
        statusWidth, statusColor);
    }
    ctx.restore();
  }

  // 현장 테스트용 정류장 판정. 캔버스에 그리므로 오버레이 영상에도 남는다.
  function stopDiagnostic(event, crosswalk, walkingSurface) {
    const state = event?.status || "unavailable";
    const recordedUndetected = state === "not_detected" && event?.arrival_recorded;
    const basis = { left: "좌측", right: "우측", bottom: "하단" }[event?.basis];
    const position = basis ? `${basis} ` : "";
    const color = state === "nearby" ? "#57d7ab"
      : state === "candidate" ? "#ffce73" : "#c2ceda";
    const label = state === "nearby" ? `${position}정류장 ${event.held ? "근접 유지 · 재확인 중" : "근접 추정"}`
      : state === "candidate" ? event?.arrival_recorded ? "정류장 후보 재확인 중"
        : event.held ? `${position}정류장 후보 유지`
          : `${position}정류장 후보 ${event.observations}/${event.required_observations}`
      : recordedUndetected ? "정류장 도착 기록 있음 · 현재 화면에서 미검출"
      : state === "not_detected" ? "정류장 미검출" : "정류장 판정 보류";
    const lines = recordedUndetected ? ["정류장 도착 기록 있음 ·", "현재 화면에서 미검출"] : [label];
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
    const bannerWidth = Math.min(canvas.width - 16,
      Math.max(...lines.map(line => ctx.measureText(line).width)) + 22);
    const x = canvas.width - bannerWidth - 8;
    const bannerHeight = lines.length === 2 ? 54 : 36;
    const statusHeight = Math.max(26, canvas.height / 24);
    const rightBadges = [crosswalk?.status, walkingSurface?.status]
      .filter(status => status && status !== "disabled").length;
    const actionHeight = Math.max(26, canvas.height / 24) * 2;
    const y = 8 + Math.max(actionHeight, rightBadges * (statusHeight + 8)) + 8;
    ctx.fillStyle = "#08131feb";
    ctx.fillRect(x, y, bannerWidth, bannerHeight);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, bannerWidth, bannerHeight);
    ctx.fillStyle = color;
    lines.forEach((line, index) => ctx.fillText(line, x + 11, y + 21 + index * 19));
    ctx.restore();
  }

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

  /** 안전성·전환 확인을 마친 행동과 실제 재생 중인 음성을 표시한다. */
  function actionStatus(event) {
    const action = (event && "voice_action" in event ? event.voice_action : event?.last_action) ?? "none";
    const voice = event?.voice_playback_action ?? "none";
    const motion = event?.stationarity?.status ?? "unavailable";
    const lines = [`ACTION: ${action}`, `VOICE: ${voice}`, `MOTION: ${motion}`];
    ctx.font = `bold ${Math.max(13, canvas.width / 38)}px system-ui`;
    const padding = 8;
    const lineHeight = Math.max(26, canvas.height / 24);
    const height = lineHeight * lines.length;
    const width = Math.max(...lines.map(text => ctx.measureText(text).width)) + padding * 2;
    const x = 8;
    const y = 8;
    ctx.fillStyle = "rgba(24, 24, 24, 0.88)";
    ctx.fillRect(x, y, width, height);
    ctx.fillStyle = "#ffffff";
    lines.forEach((text, index) => ctx.fillText(
      text, x + padding, y + lineHeight * (index + 1) - 8));
  }

  /** 서버 판정에 사용한 고정 ROI를 보라색 테두리로 표시한다. */
  function crosswalkRoi(event) {
    const roi = event?.crosswalk_roi;
    const values = [roi?.left, roi?.right, roi?.top, roi?.bottom];
    if (!values.every(Number.isFinite)) return;
    const [left, right, top, bottom] = values;
    polygon([[left, top], [right, top], [right, bottom], [left, bottom]],
      "#9b5de5", "#9b5de50d");
  }

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

  /** 새 결과가 도착했을 때 이전 프레임의 비동기 이미지 로딩을 무효화한다. */
  function render(result, onDrawn = () => {}) {
    const current = ++version;
    canvas.hidden = false;
    size(result.image_width, result.image_height);
    const draw = mask => {
      if (current !== version) return onDrawn("superseded");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const busMode = result.bus_mode === true
        || (result.bus_mode !== false && Boolean(result.bus?.event?.target_route));
      const busAge = Number.isFinite(result.bus?.captured_at_ms)
        && typeof performance !== "undefined" && Number.isFinite(performance.timeOrigin)
        ? performance.timeOrigin + performance.now() - result.bus?.captured_at_ms : Infinity;
      if (busMode) {
        busScene(result, busAge);
        return onDrawn("drawn");
      }
      // Use the same freshness window as live guidance, including network and mask decoding.
      const maxAgeMs = window.GConfig?.get?.().audio?.guidance?.max_age_ms ?? 1500;
      if (Number.isFinite(result.captured_at_ms) && typeof performance !== "undefined"
          && Number.isFinite(performance.timeOrigin)
          && performance.timeOrigin + performance.now() - result.captured_at_ms >= maxAgeMs) {
        return onDrawn("stale");
      }
      if (mask) ctx.drawImage(mask, 0, 0, canvas.width, canvas.height);
      const obstacles = result.walking?.event?.enabled !== false && result.boarding?.obstacle_detection_enabled !== false;
      const roi = obstacles ? result.walking?.event?.roi || {} : {};
      for (const points of roi.corridor_polygons || [roi.corridor_polygon])
        polygon(points, "#4ce3fa", "#4ce3fa20");
      polygon(roi.immediate_polygon, "#ff88ba", "#ff88ba24");
      if (obstacles) directionBoundaries();
      crosswalkRoi(result.crosswalk?.event);
      walkingSurfaceRoi(result.walking_surface?.event);
      if (obstacles) boxes(result.walking?.detections, "walking");
      boxes(result.traffic?.detections, "traffic");
      // OCR finishes independently; only boxes from a recent capture belong on the live camera.
      if (Number.isFinite(result.bus?.captured_at_ms) && busAge >= 0 && busAge <= 400)
        boxes(result.bus?.detections, "bus");
      crosswalkSafety(result.crosswalk?.event);
      if (obstacles) actionStatus(result.walking?.event);
      crosswalkStatus(result.crosswalk?.event);
      walkingSurfaceStatus(result.walking_surface?.event);
      if (obstacles) stopDiagnostic(result.stop_proximity,
        result.crosswalk?.event, result.walking_surface?.event);
      onDrawn("drawn");
    };
    if (result.bus_mode === true || (result.bus_mode !== false && result.bus?.event?.target_route)
        || !result.walking?.mask_png) return draw(null);
    const mask = new Image();
    mask.onload = () => draw(mask);
    mask.onerror = () => draw(null);
    mask.src = `data:image/png;base64,${result.walking.mask_png}`;
  }

  /** Live bus screen: existing guidance card, compact bus/number labels and step line. */
  function renderBusBoxes(result) {
    const busMode = result?.bus_mode === true
      || (result?.bus_mode !== false && Boolean(result?.bus?.event?.target_route));
    if (!busMode || result.paused) { clear(); return; }
    version++;
    canvas.hidden = false;
    size(result.image_width, result.image_height);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const age = Number.isFinite(result.bus?.captured_at_ms)
      && typeof performance !== "undefined" && Number.isFinite(performance.timeOrigin)
      ? performance.timeOrigin + performance.now() - result.bus.captured_at_ms : Infinity;
    busScene(result, age, "slim");
  }

  /** 카메라 영상만 남기고 분석 결과를 지운다. */
  function clear() {
    version++;
    canvas.hidden = true;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  /** 저장할 추론 프레임에 화면과 같은 오버레이 픽셀을 사용한다. */
  function snapshot() {
    return canvas.toDataURL("image/png").split(",", 2)[1];
  }

  return { size, render, renderBusBoxes, clear, snapshot };
})();
