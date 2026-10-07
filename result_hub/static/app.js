"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    source: "", query: "", sessions: [], logs: [], selected: null, selectedLog: null,
    listSignature: "", logSignature: "", sourceSignature: "", refreshVersion: 0,
    detailVersion: 0, logVersion: 0, refreshing: false,
  };
  const dateFormat = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const numberFormat = new Intl.NumberFormat("ko-KR");
  const statusNames = {
    uploading: "업로드 중", pending: "변환 대기", rendering: "영상 변환 중",
    processing: "처리 중", ready: "영상 준비됨", no_frames: "추론 프레임 없음",
    failed: "변환 실패", unknown: "상태 확인 필요",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function scalar(value, fallback = "—") {
    if (value === undefined || value === null || value === "") return fallback;
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
  function date(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "시각 정보 없음" : dateFormat.format(parsed);
  }
  function age(value) {
    if (!value) return "수신 기록 없음";
    const elapsed = Math.max(0, Date.now() - new Date(value).getTime());
    if (!Number.isFinite(elapsed)) return "수신 시각 확인 필요";
    if (elapsed < 60_000) return "1분 이내 수신";
    if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}분 전 수신`;
    if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}시간 전 수신`;
    return `${Math.floor(elapsed / 86_400_000)}일 전 수신`;
  }
  function bytes(value) {
    const size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "크기 정보 없음";
    if (size < 1024) return `${size} B`;
    if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 ** 2).toFixed(1)} MB`;
  }
  function count(value) { return numberFormat.format(Number(value) || 0); }
  function key(item) { return JSON.stringify([item.source_id, item.session_id]); }
  function logKey(item) { return JSON.stringify([item.source_id, item.path]); }
  function sameOriginUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !["http:", "https:"].includes(url.protocol)) return null;
      return url;
    } catch { return null; }
  }
  function downloadLink(urlValue, label = "다운로드") {
    const url = sameOriginUrl(urlValue);
    if (!url) return null;
    url.searchParams.set("download", "true");
    const link = el("a", "", label);
    link.href = url.toString();
    link.download = "";
    return link;
  }
  function button(label, action, className = "") {
    const node = el("button", className, label);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  function notice(message) {
    $("notice").textContent = message || "";
    $("notice").hidden = !message;
  }
  async function api(path) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      const response = await fetch(path, {
        credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (response.status === 401 || response.status === 403) {
        throw new Error("인증을 확인할 수 없습니다. 페이지를 다시 열고 공용 조회 계정으로 로그인하세요.");
      }
      if (!response.ok) throw new Error(`기록을 불러오지 못했습니다 (HTTP ${response.status}). 잠시 후 새로고침해 주세요.`);
      return await response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("서버 응답이 지연되고 있습니다. 연결을 확인한 뒤 다시 시도하세요.");
      if (error instanceof TypeError) throw new Error("수집 서버에 연결할 수 없습니다. 서버와 네트워크 연결을 확인하세요.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function empty(container, title, text) {
    const wrap = el("div", "empty");
    wrap.append(el("h3", "", title), el("p", "", text));
    container.replaceChildren(wrap);
  }

  function renderSources(sources) {
    const fragment = document.createDocumentFragment();
    for (const source of sources) {
      const card = el("article", "source-card");
      const title = el("strong", "", scalar(source.source_id));
      title.append(el("span", "source-count", `${count(source.session_count)}개 테스트`));
      card.append(title, el("div", "received", `마지막 수신 ${date(source.last_received_at)}`), el("div", "source-age", age(source.last_received_at)));
      fragment.append(card);
    }
    if (!sources.length) fragment.append(el("div", "source-card source-empty", "아직 연결된 전송 서버가 없습니다. 각 서버의 전송 도구를 실행하면 여기에 표시됩니다."));
    $("sources").replaceChildren(fragment);
    const ids = sources.map((source) => String(source.source_id)).sort();
    if (state.source && !ids.includes(state.source)) ids.push(state.source);
    const signature = JSON.stringify(ids);
    if (signature !== state.sourceSignature) {
      const options = [new Option("전체 서버", ""), ...ids.map((id) => new Option(id, id))];
      $("source-filter").replaceChildren(...options);
      $("source-filter").value = state.source;
      state.sourceSignature = signature;
    }
  }
  function renderSessions() {
    const signature = JSON.stringify([state.sessions, state.selected]);
    if (signature === state.listSignature) return;
    state.listSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const item of state.sessions) {
      const selected = state.selected && key(state.selected) === key(item);
      const row = button("", () => selectSession(item), `session-item${selected ? " active" : ""}`);
      row.setAttribute("aria-pressed", selected ? "true" : "false");
      const heading = el("span", "item-heading");
      heading.append(el("span", "device", scalar(item.device_name, "이름 없는 기기")), el("span", "source-tag", scalar(item.source_id)));
      row.append(heading, el("span", "item-date", date(item.started_at)), el("span", "item-note", scalar(item.note, "메모 없음")));
      const captureStatus = item.ended_at ? "촬영 종료" : "종료 기록 없음";
      row.append(el("span", "item-status", `${captureStatus} · 클립 ${count(item.clip_count)}개 · 변환 완료 ${count(item.ready_clip_count)}개`));
      fragment.append(row);
    }
    if (!state.sessions.length) fragment.append(el("p", "list-empty", state.query || state.source ? "조건에 맞는 테스트 기록이 없습니다." : "아직 수신한 테스트 기록이 없습니다."));
    $("session-list").replaceChildren(fragment);
    $("session-count").textContent = `${count(state.sessions.length)}개 기록`;
  }
  function metadataItem(label, value, wide = false) {
    const pair = el("div", wide ? "wide" : "");
    pair.append(el("dt", "", label), el("dd", "", scalar(value)));
    return pair;
  }
  function sectionHeading(title, note = "") {
    const heading = el("div", "section-heading");
    heading.append(el("h3", "", title));
    if (note) heading.append(el("span", "muted", note));
    return heading;
  }
  function videoPane(label, urlValue, placeholder) {
    const pane = el("div", "video-pane");
    const heading = el("div", "video-label", label);
    const url = sameOriginUrl(urlValue);
    if (url) heading.append(downloadLink(url.toString()));
    pane.append(heading);
    if (url) {
      const video = el("video");
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.src = url.toString();
      video.setAttribute("aria-label", label);
      pane.append(video);
      video.addEventListener("error", () => {
        if (!pane.querySelector(".video-error")) pane.append(el("p", "clip-note video-error", "브라우저에서 영상을 재생할 수 없습니다. 다운로드하여 확인해 주세요."));
      });
    } else pane.append(el("div", "video-empty", placeholder));
    return pane;
  }
  function renderClip(clip) {
    const card = el("article", "clip-card");
    const status = Object.hasOwn(statusNames, clip.state) ? clip.state : "unknown";
    const heading = el("div", "clip-heading");
    const waitingForFiles = status === "ready" && (!sameOriginUrl(clip.original_url) || !sameOriginUrl(clip.inference_url));
    heading.append(el("strong", "", `클립 ${scalar(clip.clip_id)}`), el("span", `badge ${waitingForFiles ? "pending" : status}`, waitingForFiles ? "영상 전송 대기" : statusNames[status]));
    const duration = Number(clip.duration_ms);
    const timing = Number.isFinite(duration) ? `${(duration / 1000).toFixed(1)}초 · ` : "";
    heading.append(el("span", "muted", `${timing}분석 프레임 ${count(clip.frame_count)}개`));
    card.append(heading);
    const videos = el("div", "video-grid");
    videos.append(videoPane("원본 영상", clip.original_url, "원본 영상이 아직 수신되지 않았습니다."));
    videos.append(videoPane("분석 영상", clip.inference_url, status === "no_frames" ? "선택된 추론 프레임이 없어 분석 영상이 없습니다." : status === "failed" ? "영상 변환에 실패했습니다. 오류를 확인하세요." : "분석 영상 변환 또는 파일 전송을 기다리고 있습니다."));
    card.append(videos);
    if (clip.error) card.append(el("p", "clip-note clip-error", `오류: ${scalar(clip.error)}`));
    if (waitingForFiles) {
      card.append(el("p", "clip-note", "원본 서버의 변환은 완료되었지만 중앙 서버에 일부 영상이 아직 도착하지 않았습니다."));
    }
    const manifestLink = downloadLink(clip.manifest_url, "클립 상태 파일 다운로드");
    if (manifestLink) { const note = el("p", "clip-note"); note.append(manifestLink); card.append(note); }
    return card;
  }
  function previewPath(source, path) {
    return `/api/preview/${encodeURIComponent(source)}/${String(path).split("/").map(encodeURIComponent).join("/")}?lines=200`;
  }
  async function loadPreview(container, source, file, stillCurrent = () => true) {
    const wrap = el("section", "preview");
    const heading = el("div", "preview-heading");
    heading.append(el("strong", "", scalar(file.path)), button("닫기", () => wrap.remove(), "small-button"));
    const info = el("p", "", "최근 200줄을 불러오는 중…");
    const output = el("pre", "", "");
    wrap.append(heading, info, output);
    container.replaceChildren(wrap);
    try {
      const result = await api(previewPath(source, file.path));
      if (!stillCurrent() || !wrap.isConnected) return;
      const text = String(result.text ?? "");
      output.textContent = text.slice(-150_000) || "파일에 기록된 내용이 없습니다.";
      info.textContent = result.truncated || text.length > 150_000 ? "파일 끝부분을 제한된 크기로 표시합니다. 전체 내용은 다운로드하여 확인하세요." : "마지막으로 수신한 파일의 최근 200줄 이하를 표시합니다.";
    } catch (error) {
      if (!stillCurrent() || !wrap.isConnected) return;
      info.textContent = error.message;
    }
  }
  function provenanceDownload(value) {
    return button("전송 환경 JSON 다운로드", () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
      const link = el("a");
      link.href = url;
      link.download = "provenance.json";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    }, "small-button");
  }
  function renderDetail(data) {
    const session = data.session || {};
    const container = $("session-detail");
    const fragment = document.createDocumentFragment();
    const heading = el("div", "detail-heading");
    const title = el("div");
    title.append(el("h2", "", scalar(session.device_name, "테스트 상세")), el("div", "muted", `${scalar(data.source_id)} / ${scalar(data.session_id)}`));
    heading.append(title, button("상세 새로고침", () => selectSession(state.selected)));
    fragment.append(heading);
    const metadata = el("dl", "metadata");
    metadata.append(metadataItem("촬영 시작", date(session.started_at)), metadataItem("촬영 종료", session.ended_at ? date(session.ended_at) : "종료 기록 없음"), metadataItem("분석 프레임", `${count(session.frame_count)}개`), metadataItem("메모", scalar(session.note, "메모 없음"), true));
    fragment.append(metadata);
    fragment.append(sectionHeading("영상 클립", "상세 새로고침으로 최신 수신 상태 확인"));
    const clips = Array.isArray(data.clips) ? data.clips : [];
    if (!clips.length) fragment.append(el("p", "inline-empty", "아직 수신한 클립이 없습니다. 영상 선택·업로드 여부와 전송 도구 상태를 확인하세요. 촬영 종료만으로 영상 저장이 완료되지는 않습니다."));
    for (const clip of clips) fragment.append(renderClip(clip));
    const allFiles = Array.isArray(data.files) ? data.files : [];
    const legacyOriginal = allFiles.find((file) => String(file.path).endsWith("/camera.mp4"));
    const legacyInference = allFiles.find((file) => String(file.path).endsWith("/camera_overlay.mp4"));
    if (legacyOriginal || legacyInference) {
      fragment.append(sectionHeading("세션 영상", "이전 저장 형식"));
      const videos = el("div", "video-grid clip-card");
      videos.append(videoPane("원본 영상", legacyOriginal?.url, "원본 영상이 아직 수신되지 않았습니다."), videoPane("분석 영상", legacyInference?.url, "분석 영상이 아직 수신되지 않았습니다."));
      fragment.append(videos);
    }
    fragment.append(sectionHeading("분석 결과 · 기록 파일"));
    const files = allFiles.filter((file) => !/\.(mp4|webm|mov|jpg|jpeg|png)$/i.test(String(file.path)));
    const list = el("div", "file-list");
    const previewContainer = el("div", "file-preview");
    const selectionKey = key(data);
    for (const file of files) {
      const row = el("div", "file-row");
      const info = el("div", "file-info");
      info.append(el("span", "file-name", scalar(file.path)), el("span", "file-size", `${bytes(file.size)} · 수신 ${date(file.received_at)}`));
      const actions = el("div", "file-actions");
      if (/\.(jsonl|json|log|txt|csv|yaml|yml)$/i.test(String(file.path))) {
        actions.append(button("미리보기", () => loadPreview(previewContainer, data.source_id, file, () => state.selected && key(state.selected) === selectionKey), "small-button"));
      }
      const download = downloadLink(file.url);
      if (download) actions.append(download);
      row.append(info, actions);
      list.append(row);
    }
    if (!files.length) fragment.append(el("p", "inline-empty", "아직 수신한 분석 결과 파일이 없습니다."));
    else fragment.append(list);
    fragment.append(previewContainer);
    fragment.append(sectionHeading("실행 · 전송 환경"));
    if (data.provenance && typeof data.provenance === "object") {
      fragment.append(el("p", "provenance-note", "서버 시작 시 디스크에서 관측한 코드·설정과 모델 파일 정보를 확인할 수 있습니다. 실제 메모리에 로딩된 버전을 보증하는 기록은 아닙니다."), provenanceDownload(data.provenance));
    } else fragment.append(el("p", "provenance-note", "아직 수신한 실행 환경 정보가 없습니다."));
    container.replaceChildren(fragment);
  }
  async function selectSession(item) {
    if (!item) return;
    state.selected = { source_id: item.source_id, session_id: item.session_id };
    renderSessions();
    const version = ++state.detailVersion;
    empty($("session-detail"), "테스트 기록을 불러오는 중", "영상과 결과 파일의 수신 상태를 확인하고 있습니다.");
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(item.source_id)}/${encodeURIComponent(item.session_id)}`);
      if (version !== state.detailVersion) return;
      renderDetail(data);
    } catch (error) {
      if (version !== state.detailVersion) return;
      empty($("session-detail"), "상세 기록을 불러오지 못했습니다", error.message);
      $("session-detail").append(button("다시 시도", () => selectSession(item)));
    }
  }
  function renderLogs() {
    const signature = JSON.stringify([state.logs, state.selectedLog]);
    if (signature === state.logSignature) return;
    state.logSignature = signature;
    const fragment = document.createDocumentFragment();
    for (const file of state.logs) {
      const active = state.selectedLog && logKey(file) === logKey(state.selectedLog);
      const row = button("", () => selectLog(file), `log-item${active ? " active" : ""}`);
      row.setAttribute("aria-pressed", active ? "true" : "false");
      row.append(el("strong", "", scalar(file.source_id)), el("span", "", scalar(file.path)), el("span", "", `${bytes(file.size)} · ${age(file.received_at)}`));
      fragment.append(row);
    }
    if (!state.logs.length) fragment.append(el("p", "list-empty", "아직 수신한 서버 로그가 없습니다."));
    $("log-list").replaceChildren(fragment);
  }
  function selectLog(file) {
    state.selectedLog = file;
    renderLogs();
    const version = ++state.logVersion;
    const container = $("log-detail");
    const actions = el("div", "log-actions");
    actions.append(button("로그 새로고침", () => selectLog(state.logs.find((item) => logKey(item) === logKey(file)) || file), "small-button"));
    const download = downloadLink(file.url, "전체 로그 다운로드");
    if (download) actions.append(download);
    actions.append(el("span", "muted", `마지막 수신 ${date(file.received_at)}`));
    const previewContainer = el("div");
    container.replaceChildren(actions, previewContainer);
    loadPreview(previewContainer, file.source_id, file, () => version === state.logVersion);
  }
  async function refresh(automatic = false) {
    if (automatic && (document.hidden || state.refreshing)) return;
    const version = ++state.refreshVersion;
    state.refreshing = true;
    $("refresh").disabled = true;
    const parameters = new URLSearchParams();
    if (state.source) parameters.set("source_id", state.source);
    const logParameters = new URLSearchParams(parameters);
    if (state.query) parameters.set("q", state.query);
    try {
      const [sources, sessions, logs] = await Promise.all([
        api("/api/sources"), api(`/api/sessions?${parameters}`), api(`/api/logs?${logParameters}`),
      ]);
      if (version !== state.refreshVersion) return;
      state.sessions = (Array.isArray(sessions.sessions) ? sessions.sessions : []).sort((a, b) => (Date.parse(b.started_at) || 0) - (Date.parse(a.started_at) || 0));
      state.logs = Array.isArray(logs.logs) ? logs.logs : [];
      renderSources(Array.isArray(sources.sources) ? sources.sources : []);
      renderSessions();
      renderLogs();
      $("last-refresh").textContent = `목록 확인 ${date(new Date().toISOString())}`;
      notice("");
    } catch (error) {
      if (version === state.refreshVersion) notice(`${error.message} 표시 중인 기록은 이전에 불러온 내용일 수 있습니다.`);
    } finally {
      if (version === state.refreshVersion) {
        state.refreshing = false;
        $("refresh").disabled = false;
      }
    }
  }
  function changeTab(name) {
    for (const tabName of ["sessions", "logs"]) {
      const active = tabName === name;
      $(`${tabName}-tab`).classList.toggle("active", active);
      $(`${tabName}-tab`).setAttribute("aria-selected", active ? "true" : "false");
      $(`${tabName}-tab`).tabIndex = active ? 0 : -1;
      $(`${tabName}-panel`).hidden = !active;
    }
  }
  function resetSelection() {
    state.selected = null;
    state.selectedLog = null;
    state.detailVersion++;
    state.logVersion++;
    empty($("session-detail"), "테스트 기록을 선택하세요", "원본 영상과 분석 영상을 비교하고 결과 파일을 확인할 수 있습니다.");
    empty($("log-detail"), "로그 파일을 선택하세요", "최근 200줄을 확인하거나 전체 파일을 다운로드할 수 있습니다.");
  }

  $("refresh").addEventListener("click", () => refresh());
  $("source-filter").addEventListener("change", () => {
    state.source = $("source-filter").value;
    resetSelection();
    refresh();
  });
  let searchTimer;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = $("search").value.trim();
      refresh();
    }, 300);
  });
  for (const name of ["sessions", "logs"]) {
    $(`${name}-tab`).addEventListener("click", () => changeTab(name));
    $(`${name}-tab`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "sessions" : event.key === "End" ? "logs" : name === "sessions" ? "logs" : "sessions";
      changeTab(next);
      $(`${next}-tab`).focus();
    });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  refresh();
  setInterval(() => refresh(true), 20_000);
})();
