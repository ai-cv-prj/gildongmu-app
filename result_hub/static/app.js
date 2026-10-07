"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    source: "", query: "", category: "", categoryBusy: false, categoryDrafts: new Map(), sessions: [], logs: [], selected: null, selectedLog: null,
    listSignature: "", logSignature: "", sourceSignature: "", refreshVersion: 0,
    detailVersion: 0, logVersion: 0, refreshing: false, detailData: null,
    adminEnabled: false, admin: false, adminBusy: false, adminVersion: 0,
    checked: new Set(), trash: [], trashVersion: 0, tab: "sessions", filterPending: false,
  };
  // Keep this credential only in page memory, never in browser storage or URLs.
  let adminPassword = "";
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

  const categoryNames = { obstacle: "장애물", traffic_light: "신호등", bus: "버스" };

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
  async function api(path, { method = "GET", admin = false, password = adminPassword, body } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      const response = await fetch(path, {
        method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        body: body === undefined ? undefined : JSON.stringify(body),
        headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json", "X-Hub-Request": "categories" }), ...(admin ? { "X-Hub-Admin-Password": password } : {}) },
      });
      let result;
      try { result = await response.json() || {}; } catch { result = {}; }
      if (response.status === 401 || response.status === 403) {
        if (admin) {
          if (state.admin && password === adminPassword) endAdmin();
          throw new Error(typeof result.detail === "string" ? result.detail : "관리자 인증을 확인할 수 없습니다. 관리자 비밀번호를 다시 입력하세요.");
        }
        throw new Error("인증을 확인할 수 없습니다. 페이지를 다시 열고 공용 조회 계정으로 로그인하세요.");
      }
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `요청을 처리하지 못했습니다 (HTTP ${response.status}). 잠시 후 다시 시도해 주세요.`);
      return result;
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
    renderSelection();
    const signature = JSON.stringify([state.sessions, state.selected, state.admin, state.adminBusy, state.refreshing, state.filterPending, [...state.checked]]);
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
      const badges = categoryBadges(item.categories);
      if (item.categories?.length && item.unclassified_clip_count) badges.append(el("span", "category-badge category-unclassified", `미분류 ${count(item.unclassified_clip_count)}`));
      row.append(badges);
      if (state.category) row.append(el("span", "item-status", `선택한 종류의 클립 ${count(item.matched_clip_count)}개`));
      const captureStatus = item.ended_at ? "촬영 종료" : "종료 기록 없음";
      row.append(el("span", "item-status", `${captureStatus} · 클립 ${count(item.clip_count)}개 · 변환 완료 ${count(item.ready_clip_count)}개`));
      if (state.admin) {
        const wrap = el("div", "session-select-row");
        const checkbox = el("input", "session-checkbox");
        checkbox.type = "checkbox";
        checkbox.checked = state.checked.has(key(item));
        checkbox.disabled = state.adminBusy || state.refreshing || state.filterPending;
        checkbox.setAttribute("aria-label", `${scalar(item.source_id)} / ${scalar(item.session_id)} 삭제 대상으로 선택`);
        checkbox.addEventListener("change", () => {
          if (!state.admin || state.adminBusy || state.refreshing || state.filterPending) return;
          if (checkbox.checked) state.checked.add(key(item));
          else state.checked.delete(key(item));
          renderSelection();
        });
        wrap.append(checkbox, row);
        fragment.append(wrap);
      } else fragment.append(row);
    }
    if (!state.sessions.length) fragment.append(el("p", "list-empty", state.query || state.source || state.category ? "조건에 맞는 테스트 기록이 없습니다." : "아직 수신한 테스트 기록이 없습니다."));
    $("session-list").replaceChildren(fragment);
    $("session-count").textContent = `${count(state.sessions.length)}개 기록`;
  }
  function categoryBadges(values) {
    const wrap = el("span", "category-badges");
    const categories = Array.isArray(values) ? values.filter(value => categoryNames[value]) : [];
    for (const value of categories) wrap.append(el("span", `category-badge category-${value}`, categoryNames[value]));
    if (!categories.length) wrap.append(el("span", "category-badge category-unclassified", "미분류"));
    return wrap;
  }
  function clipMatches(clip) {
    return !state.category || (state.category === "unclassified" ? !clip.categories?.length : (clip.categories || []).includes(state.category));
  }
  function updateClipCards(data) {
    if (!state.selected || key(state.selected) !== key(data)) return;
    for (const card of $("session-detail").querySelectorAll(".clip-card")) {
      const clip = data.clips.find(item => item.category_key === card.categoryKey);
      if (!clip) continue;
      card.hidden = !clipMatches(clip);
      if (card.hidden) for (const video of card.querySelectorAll("video")) video.pause();
      card.querySelector(".clip-category-badges").replaceChildren(categoryBadges(clip.categories));
    }
    const note = $("session-detail").querySelector(".clip-filter-note");
    if (note) note.textContent = state.category ? `${categoryNames[state.category] || "미분류"} 클립 ${data.clips.filter(clipMatches).length}개 / 전체 ${data.clips.length}개 · 다른 종류는 영상 종류 필터에서 선택하세요.` : "클립마다 종류를 따로 선택하고 저장하세요.";
  }
  function clearCategoryDrafts(data) {
    const prefix = `${key(data)}:`;
    for (const recordKey of state.categoryDrafts.keys()) if (recordKey.startsWith(prefix)) state.categoryDrafts.delete(recordKey);
  }
  function categoryEditor(data, clip) {
    const form = el("form", "category-editor");
    const recordKey = `${key(data)}:${clip.category_key}`;
    const previous = state.categoryDrafts.get(recordKey);
    const draft = previous?.dirty ? previous : {
      categories: [...(clip.categories || [])], revision: clip.category_revision || 0, dirty: false,
    };
    state.categoryDrafts.set(recordKey, draft);
    const fields = el("fieldset");
    fields.append(el("legend", "", "영상 분류"));
    form.append(el("p", "muted", "이 클립에 해당하는 항목을 모두 선택하세요. 원본·분석 영상에 함께 적용됩니다."));
    const choices = [];
    for (const [value, label] of Object.entries(categoryNames)) {
      const input = el("input");
      input.type = "checkbox";
      input.value = value;
      input.checked = draft.categories.includes(value);
      input.disabled = state.categoryBusy;
      const choice = el("label", "category-choice");
      choice.append(input, el("span", "", label));
      fields.append(choice);
      choices.push(input);
      input.addEventListener("change", () => {
        draft.categories = choices.filter(node => node.checked).map(node => node.value);
        draft.dirty = true;
        state.categoryDrafts.set(recordKey, draft);
        status.textContent = "변경한 분류를 저장해 주세요.";
      });
    }
    const status = el("p", "category-status");
    status.setAttribute("role", "status");
    const save = el("button", "category-save", "분류 저장");
    save.type = "submit";
    save.disabled = state.categoryBusy;
    form.append(fields, el("p", "muted", "중복 선택 가능 · 모두 해제하여 저장하면 미분류"), save, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (state.categoryBusy || state.adminBusy) return;
      state.categoryBusy = true;
      save.disabled = true;
      choices.forEach(node => { node.disabled = true; });
      status.textContent = "분류를 저장하고 있습니다…";
      try {
        const result = await api(`/api/sessions/${encodeURIComponent(data.source_id)}/${encodeURIComponent(data.session_id)}/clips/${encodeURIComponent(clip.category_key)}/categories`, {
          method: "PUT", body: { categories: draft.categories, revision: draft.revision },
        });
        Object.assign(clip, result.item);
        draft.categories = [...result.item.categories];
        draft.revision = result.item.category_revision;
        draft.dirty = false;

        // Update a replacement detail view as well, but never another selected record.
        if (state.detailData && key(state.detailData) === key(data)) {
          const current = state.detailData.clips.find(item => item.category_key === clip.category_key);
          if (current) Object.assign(current, result.item);
          updateClipCards(state.detailData);
        }
        status.textContent = "분류를 저장했습니다.";
        await refresh();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        state.categoryBusy = false;
        for (const node of document.querySelectorAll(".category-save")) node.disabled = false;
        for (const node of document.querySelectorAll(".category-choice")) {
          const input = node.querySelector("input");
          if (input) input.disabled = false;
        }
      }
    });
    return form;
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
  function renderClip(clip, data) {
    const card = el("article", "clip-card");
    card.categoryKey = clip.category_key;
    card.hidden = !clipMatches(clip);
    const status = Object.hasOwn(statusNames, clip.state) ? clip.state : "unknown";
    const heading = el("div", "clip-heading");
    const waitingForFiles = status === "ready" && (!sameOriginUrl(clip.original_url) || !sameOriginUrl(clip.inference_url));
    heading.append(el("strong", "", clip.legacy ? "세션 영상 (이전 저장 형식)" : `클립 ${scalar(clip.clip_id)}`), el("span", `badge ${waitingForFiles ? "pending" : status}`, waitingForFiles ? "영상 전송 대기" : statusNames[status]));
    const duration = Number(clip.duration_ms);
    const timing = Number.isFinite(duration) ? `${(duration / 1000).toFixed(1)}초 · ` : "";
    heading.append(el("span", "muted", `${timing}분석 프레임 ${count(clip.frame_count)}개`));
    const badges = el("div", "clip-category-badges");
    badges.append(categoryBadges(clip.categories));
    card.append(heading, badges, categoryEditor(data, clip));
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
    state.detailData = data;
    const session = data.session || {};
    const container = $("session-detail");
    const fragment = document.createDocumentFragment();
    const heading = el("div", "detail-heading");
    const title = el("div");
    title.append(el("h2", "", scalar(session.device_name, "테스트 상세")), el("div", "muted", `${scalar(data.source_id)} / ${scalar(data.session_id)}`));
    const actions = el("div", "detail-actions");
    actions.append(button("상세 새로고침", () => { if (state.categoryBusy) return; clearCategoryDrafts(data); selectSession(state.selected); }));
    if (state.admin) actions.append(adminButton("테스트 기록 삭제", () => deleteSessions([{ source_id: data.source_id, session_id: data.session_id }]), "danger-button"));
    heading.append(title, actions);
    fragment.append(heading);
    const metadata = el("dl", "metadata");
    metadata.append(metadataItem("촬영 시작", date(session.started_at)), metadataItem("촬영 종료", session.ended_at ? date(session.ended_at) : "종료 기록 없음"), metadataItem("분석 프레임", `${count(session.frame_count)}개`), metadataItem("메모", scalar(session.note, "메모 없음"), true));
    fragment.append(metadata);
    fragment.append(sectionHeading("영상 클립", "상세 새로고침으로 최신 수신 상태 확인"));
    const clips = Array.isArray(data.clips) ? data.clips : [];
    const filterNote = el("p", "clip-filter-note muted");
    filterNote.setAttribute("role", "status");
    fragment.append(filterNote);
    if (!clips.length) fragment.append(el("p", "inline-empty", "아직 수신한 클립이 없습니다. 영상 선택·업로드 여부와 전송 도구 상태를 확인하세요. 촬영 종료만으로 영상 저장이 완료되지는 않습니다."));
    for (const clip of clips) fragment.append(renderClip(clip, data));
    const allFiles = Array.isArray(data.files) ? data.files : [];
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
    updateClipCards(data);
  }
  async function selectSession(item) {
    if (!item) return;
    state.selected = { source_id: item.source_id, session_id: item.session_id };
    state.detailData = null;
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
    if (state.admin && /^logs\/app\.log\.\d{4}-\d{2}-\d{2}$/.test(String(file.path))) {
      actions.append(adminButton("보관 로그 삭제", () => deleteLog(file), "danger-button small-button"));
    }
    const previewContainer = el("div");
    container.replaceChildren(actions, previewContainer);
    loadPreview(previewContainer, file.source_id, file, () => version === state.logVersion);
  }
  async function refresh(automatic = false) {
    if (automatic && (document.hidden || state.refreshing || state.adminBusy || state.categoryBusy || state.filterPending)) return;
    const version = ++state.refreshVersion;
    state.refreshing = true;
    $("refresh").disabled = true;
    renderSessions();
    const parameters = new URLSearchParams();
    if (state.source) parameters.set("source_id", state.source);
    const logParameters = new URLSearchParams(parameters);
    if (state.query) parameters.set("q", state.query);
    if (state.category) parameters.set("category", state.category);
    try {
      const [sources, sessions, logs] = await Promise.all([
        api("/api/sources"), api(`/api/sessions?${parameters}`), api(`/api/logs?${logParameters}`),
      ]);
      if (version !== state.refreshVersion) return;
      state.sessions = (Array.isArray(sessions.sessions) ? sessions.sessions : []).sort((a, b) => (Date.parse(b.started_at) || 0) - (Date.parse(a.started_at) || 0));
      state.logs = Array.isArray(logs.logs) ? logs.logs : [];
      state.checked = new Set([...state.checked].filter((value) => state.sessions.some((item) => key(item) === value)));
      if (state.selected && !state.sessions.some((item) => key(item) === key(state.selected))) clearSession();
      if (state.selectedLog && !state.logs.some((file) => logKey(file) === logKey(state.selectedLog))) clearLog();
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
        state.filterPending = false;
        $("refresh").disabled = state.adminBusy;
        renderSessions();
        renderLogs();
      }
    }
  }
  function changeTab(name) {
    if (name === "trash" && !state.admin) return;
    state.tab = name;
    for (const tabName of ["sessions", "logs", "trash"]) {
      const active = tabName === name;
      $(`${tabName}-tab`).classList.toggle("active", active);
      $(`${tabName}-tab`).setAttribute("aria-selected", active ? "true" : "false");
      $(`${tabName}-tab`).tabIndex = active ? 0 : -1;
      $(`${tabName}-panel`).hidden = !active;
    }
    if (name === "trash") refreshTrash();
  }
  function clearSession() {
    state.selected = null;
    state.detailData = null;
    state.detailVersion++;
    empty($("session-detail"), "테스트 기록을 선택하세요", "원본 영상과 분석 영상을 비교하고 결과 파일을 확인할 수 있습니다.");
  }
  function clearLog() {
    state.selectedLog = null;
    state.logVersion++;
    empty($("log-detail"), "로그 파일을 선택하세요", "최근 200줄을 확인하거나 전체 파일을 다운로드할 수 있습니다.");
  }
  function resetSelection() {
    state.checked.clear();
    clearSession();
    clearLog();
  }

  function adminStatus(message) {
    $("admin-status").textContent = message || "";
    $("admin-status").hidden = !message;
  }
  function adminButton(label, action, className = "") {
    const node = button(label, action, `admin-action ${className}`);
    node.disabled = state.adminBusy;
    return node;
  }
  function renderSelection() {
    const total = state.sessions.length;
    const selected = state.sessions.filter((item) => state.checked.has(key(item))).length;
    $("selection-toolbar").hidden = !state.admin;
    $("selection-count").textContent = `${count(selected)}개 선택`;
    $("select-all").checked = total > 0 && selected === total;
    $("select-all").indeterminate = selected > 0 && selected < total;
    $("select-all").disabled = !total || state.adminBusy || state.refreshing || state.filterPending;
    $("delete-selected").disabled = !state.admin || !selected || state.adminBusy || state.refreshing || state.filterPending;
  }
  function renderAdmin() {
    $("admin-toggle").hidden = !state.adminEnabled;
    $("admin-toggle").textContent = state.admin ? "관리자 모드 해제" : "관리자 모드";
    $("admin-toggle").setAttribute("aria-pressed", String(state.admin));
    $("admin-mode-note").hidden = !state.admin;
    $("log-admin-note").hidden = !state.admin;
    $("trash-tab").hidden = !state.admin;
    if (!state.admin && state.tab === "trash") changeTab("sessions");
    renderSessions();
    if (state.detailData) renderDetail(state.detailData);
    if (state.selectedLog) selectLog(state.selectedLog);
  }
  function endAdmin() {
    adminPassword = "";
    state.admin = false;
    state.adminVersion++;
    state.trashVersion++;
    state.checked.clear();
    state.trash = [];
    $("trash-list").replaceChildren();
    $("admin-password").value = "";
    renderAdmin();
  }
  function cancelLogin() {
    state.adminVersion++;
    $("admin-password").value = "";
    $("admin-submit").disabled = false;
    $("admin-dialog").close();
  }
  async function loginAdmin(event) {
    event.preventDefault();
    if ($("admin-submit").disabled) return;
    let password = $("admin-password").value;
    $("admin-password").value = "";
    if (!password) return;
    const version = ++state.adminVersion;
    $("admin-submit").disabled = true;
    $("admin-login-error").hidden = true;
    try {
      await api("/api/admin/login", { method: "POST", admin: true, password });
      if (version !== state.adminVersion) return;
      adminPassword = password;
      state.admin = true;
      $("admin-dialog").close();
      renderAdmin();
      adminStatus("관리자 모드가 활성화되었습니다. 정리할 테스트 또는 날짜별 보관 로그를 선택하세요.");
    } catch (error) {
      if (version !== state.adminVersion) return;
      $("admin-login-error").textContent = error.message;
      $("admin-login-error").hidden = false;
      $("admin-password").focus();
    } finally {
      password = "";
      if (version === state.adminVersion) $("admin-submit").disabled = false;
    }
  }
  function setAdminBusy(value) {
    state.adminBusy = value;
    $("admin-toggle").disabled = value;
    $("refresh").disabled = value || state.refreshing;
    $("source-filter").disabled = value;
    $("category-filter").disabled = value;
    $("search").disabled = value;
    $("trash-refresh").disabled = value;
    for (const node of document.querySelectorAll(".admin-action")) node.disabled = value;
    renderSessions();
  }
  async function mutate(jobs, successText) {
    if (!state.admin || state.adminBusy || !jobs.length) return;
    state.refreshVersion++;
    state.refreshing = false;
    setAdminBusy(true);
    let completed = 0;
    let failure = "";
    adminStatus("기록을 처리하고 있습니다…");
    try {
      for (const job of jobs) {
        try {
          await api(job.path, { method: job.method, admin: true });
          completed++;
          job.done();
        } catch (error) {
          failure = `${job.label}: ${error.message} 나머지 항목은 처리하지 않았습니다.`;
          break;
        }
      }
      await refresh();
      const trashError = state.admin ? await refreshTrash() : "";
      adminStatus(`${count(completed)} / ${count(jobs.length)}개 ${successText}${failure ? ` ${failure}` : ""}${trashError ? ` 휴지통 확인: ${trashError}` : ""}`);
    } finally { setAdminBusy(false); }
  }
  function deleteSessions(items) {
    if (!state.admin || state.adminBusy || state.refreshing || state.filterPending || !items.length) return;
    const targets = items.map((item) => ({ source_id: item.source_id, session_id: item.session_id }));
    const labels = targets.map((item) => `${item.source_id} / ${item.session_id}`);
    if (!window.confirm(`다음 테스트 ${count(targets.length)}개를 중앙 휴지통으로 이동할까요?\n\n${labels.join("\n")}\n\n각 테스트의 영상·분석 결과·이벤트가 함께 이동합니다. 팀원 PC의 로컬 원본은 유지되며, 휴지통에 있는 동안 같은 기록의 자동 재업로드는 차단됩니다. 휴지통에서 복원할 수 있습니다.`)) return;
    return mutate(targets.map((item, index) => ({
      path: `/api/admin/sessions/${encodeURIComponent(item.source_id)}/${encodeURIComponent(item.session_id)}`,
      method: "DELETE", label: labels[index], done() {
        state.sessions = state.sessions.filter((entry) => key(entry) !== key(item));
        state.checked.delete(key(item));
        if (state.selected && key(state.selected) === key(item)) clearSession();
      },
    })), "테스트 기록을 휴지통으로 이동했습니다.");
  }
  function deleteLog(file) {
    if (!state.admin || state.adminBusy || !/^logs\/app\.log\.\d{4}-\d{2}-\d{2}$/.test(String(file.path))) return;
    const label = `${file.source_id} / ${file.path}`;
    if (!window.confirm(`보관 로그를 중앙 휴지통으로 이동할까요?\n\n${label}\n\n팀원 PC의 로컬 원본은 유지되며 같은 로그의 자동 재업로드는 차단됩니다. 현재 app.log는 계속 수집됩니다. 휴지통에서 복원할 수 있습니다.`)) return;
    return mutate([{
      path: `/api/admin/logs/${encodeURIComponent(file.source_id)}/${String(file.path).split("/").map(encodeURIComponent).join("/")}`,
      method: "DELETE", label, done() {
        state.logs = state.logs.filter((item) => logKey(item) !== logKey(file));
        if (state.selectedLog && logKey(state.selectedLog) === logKey(file)) clearLog();
      },
    }], "보관 로그를 휴지통으로 이동했습니다.");
  }
  function restoreItem(item) {
    if (!state.admin || state.adminBusy) return;
    const label = `${item.source_id} / ${item.path}`;
    if (!window.confirm(`다음 기록을 복원할까요?\n\n${label}\n\n팀원들이 다시 조회할 수 있으며 자동 전송도 다시 허용됩니다.`)) return;
    return mutate([{
      path: `/api/admin/trash/${encodeURIComponent(item.id)}/restore`, method: "POST", label,
      done() { state.trash = state.trash.filter((entry) => entry.id !== item.id); renderTrash(); },
    }], "기록을 복원했습니다.");
  }
  function renderTrash() {
    const fragment = document.createDocumentFragment();
    const items = state.trash.filter((item) => !state.source || item.source_id === state.source);
    for (const item of items) {
      const row = el("article", "trash-item");
      const info = el("div", "file-info");
      info.append(el("strong", "file-name", `${item.source_id} · ${item.kind === "session" ? "테스트 기록" : "보관 로그"}`), el("span", "file-name", scalar(item.label, item.path)), el("span", "file-size", `${scalar(item.path)} · 파일 ${count(item.file_count)}개 · ${bytes(item.size)} · 삭제 ${date(item.deleted_at)}`));
      row.append(info, adminButton("복원", () => restoreItem(item), "small-button"));
      fragment.append(row);
    }
    if (!items.length) fragment.append(el("p", "list-empty", state.source ? "이 서버의 휴지통이 비어 있습니다." : "휴지통이 비어 있습니다."));
    $("trash-list").replaceChildren(fragment);
  }
  async function refreshTrash() {
    if (!state.admin) return;
    const version = ++state.trashVersion;
    $("trash-refresh").disabled = true;
    try {
      const result = await api("/api/admin/trash", { admin: true });
      if (version !== state.trashVersion || !state.admin) return;
      state.trash = Array.isArray(result.items) ? result.items : [];
      renderTrash();
    } catch (error) {
      if (version === state.trashVersion || !state.admin) adminStatus(error.message);
      return error.message;
    } finally {
      if (version === state.trashVersion) $("trash-refresh").disabled = state.adminBusy;
    }
  }

  $("admin-toggle").addEventListener("click", () => {
    if (state.adminBusy) return;
    if (state.admin) { endAdmin(); adminStatus("관리자 모드를 해제했습니다."); return; }
    $("admin-password").value = "";
    $("admin-login-error").hidden = true;
    $("admin-dialog").showModal();
    $("admin-password").focus();
  });
  $("admin-form").addEventListener("submit", loginAdmin);
  $("admin-cancel").addEventListener("click", cancelLogin);
  $("admin-dialog").addEventListener("cancel", (event) => { event.preventDefault(); cancelLogin(); });
  $("select-all").addEventListener("change", () => {
    if (!state.admin || state.adminBusy || state.refreshing || state.filterPending) return;
    state.checked = $("select-all").checked ? new Set(state.sessions.map(key)) : new Set();
    renderSessions();
  });
  $("delete-selected").addEventListener("click", () => deleteSessions(state.sessions.filter((item) => state.checked.has(key(item)))));
  $("trash-refresh").addEventListener("click", () => refreshTrash());
  $("refresh").addEventListener("click", () => { refresh(); if (state.tab === "trash") refreshTrash(); });
  function changingFilter() {
    state.refreshVersion++;
    state.refreshing = false;
    state.filterPending = true;
    resetSelection();
    state.sessions = [];
    renderSessions();
    renderLogs();
    if (state.admin) renderTrash();
  }
  $("category-filter").addEventListener("change", () => {
    state.category = $("category-filter").value;
    changingFilter();
    refresh();
  });
  $("source-filter").addEventListener("change", () => {
    state.source = $("source-filter").value;
    state.logs = [];
    changingFilter();
    refresh();
  });
  let searchTimer;
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    state.query = $("search").value.trim();
    changingFilter();
    searchTimer = setTimeout(() => {
      refresh();
    }, 300);
  });
  for (const name of ["sessions", "logs", "trash"]) {
    $(`${name}-tab`).addEventListener("click", () => changeTab(name));
    $(`${name}-tab`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const names = state.admin ? ["sessions", "logs", "trash"] : ["sessions", "logs"];
      const offset = event.key === "ArrowLeft" ? -1 : 1;
      const next = event.key === "Home" ? names[0] : event.key === "End" ? names[names.length - 1] : names[(names.indexOf(name) + offset + names.length) % names.length];
      changeTab(next);
      $(`${next}-tab`).focus();
    });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(true); });
  api("/api/capabilities").then((result) => {
    state.adminEnabled = result.admin_enabled === true;
    renderAdmin();
  }).catch(() => {});
  refresh();
  setInterval(() => refresh(true), 20_000);
})();
