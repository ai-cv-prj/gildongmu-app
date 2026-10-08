/** Accessible view only; the application owns camera, session and bus state. */
(() => {
  const CAMERA_SCREENS = new Set(["walk", "signal", "search", "approach", "arrived"]);
  const BUS_SCREENS = new Set(["search", "approach", "arrived"]);
  const SCREENS = new Set(["welcome", "home", "type", "input", "end", ...CAMERA_SCREENS]);
  const DEFAULT_CUES = {
    walk: { label: "이동 안내", title: "주변을 살피며 이동해 주세요.", copy: "보행 위험과 신호를 확인하고 있어요.", icon: "walk", tone: "walk" },
    signal: { label: "보행자 신호", title: "신호를 확인하고 있어요.", copy: "주변과 보행자 신호를 확인해 주세요.", icon: "hand", tone: "signal" },
    search: { label: "목표 버스 탐색", title: "버스를 찾고 있어요.", copy: "버스가 오는 쪽을 바라보세요.", icon: "scan", tone: "search" },
    approach: { label: "버스 도착정보", title: "버스가 곧 도착할 예정이에요.", copy: "카메라로 목표 버스 번호를 확인하고 있어요.", icon: "bus", tone: "approach" },
    arrived: { label: "버스 도착정보", title: "도착으로 표시됩니다.", copy: "카메라로 버스 번호와 주변을 확인해 주세요. 탑승 후 종료를 눌러 주세요.", icon: "check", tone: "arrived" },
  };
  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const updateText = (element, value) => { if (element.textContent !== value) element.textContent = value; };

  function create({ onAction = () => {}, onSubmitRoute = () => {}, onRateChange = () => {},
    onTextScaleChange = () => {}, onStationChange = () => {}, onSpeak = () => {} } = {}) {
    let screen = "welcome", paused = false, busy = false;
    let obstacleDetection = true;
    let bus = {}, lastResult = null, currentCue = DEFAULT_CUES.walk;
    let micMode = null, destroyed = false, stationCount = 0, speechSession = false;
    let clipState = { active: false, count: 0, max: 3, available: false };
    let keypad = null, lastActionAt = -Infinity;
    const listeners = [];
    const cameraPanel = document.querySelector('[data-panel="camera"]');
    const film = window.GildongmuFilm.mount($("opening-film"), $("film-caption"), $("ending-film"), $("input-art"));
    function bind(element, event, callback) {
      element.addEventListener(event, callback);
      listeners.push(() => element.removeEventListener(event, callback));
    }
    function invoke(callback, ...args) {
      try {
        const result = callback(...args);
        if (result && typeof result.catch === "function") result.catch(error => setStatus(error.message || "요청을 완료하지 못했어요."));
      } catch (error) { setStatus(error.message || "요청을 완료하지 못했어요."); }
    }
    function announce(message) { updateText($("announcer"), String(message || "")); }
    function setStatus(message) {
      const value = String(message || "");
      updateText($("app-status"), value);
      updateText($("status"), value);
      $("app-status").classList.toggle("sr-only", !/실패|없습니다|없어요|확인해 주세요|권한|오류/.test(value));
    }
    function setRoute(value, { syncKeypad = true } = {}) {
      const route = String(value || "").slice(0, 30);
      $("bus-number").value = route;
      if (syncKeypad) keypad?.setValue(route);
      all("[data-route]").forEach(element => updateText(element, route || "—"));
      updateText($("camera-route"), route ? `${route}번` : "");
      $("camera-route").dataset.long = String(route.length > 5);
    }
    function readRoute() { return $("bus-number").value.trim(); }
    function routeError(message) {
      updateText($("route-error"), String(message || ""));
      $("route-error").hidden = !message;
      $("bus-number").setAttribute("aria-invalid", String(Boolean(message)));
      keypad?.setError(String(message || ""));
    }
    function hazardCue(result) {
      if (!result) return null;
      const age = Date.now() - result.captured_at_ms;
      if (!Number.isFinite(age) || age < -1000 || age >= 1500) return null;
      const walking = obstacleDetection && result.walking?.event?.enabled !== false ? result.walking?.event || {} : {};
      const crosswalk = result.crosswalk?.event || {};
      const surface = obstacleDetection ? result.walking_surface?.event || {} : {};
      const signal = result.traffic?.event || {};
      const walkingText = walking.voice_event?.text || walking.voice_text;
      const action = walking.voice_event?.action || walking.voice_action || walking.last_action;
      if (walking.level === "danger" && action === "stop" && walkingText) {
        return { label: "보행 위험", title: walkingText, copy: "잠시 멈추고 주변을 확인해 주세요.", icon: "alert", tone: "walk" };
      }
      if (crosswalk.voice_text && (crosswalk.repeat || /^(outside|align)_/.test(crosswalk.status || ""))) {
        return { label: "횡단보도 안내", title: crosswalk.voice_text, copy: "횡단보도 안에서 주변을 확인해 주세요.", icon: "alert", tone: "walk" };
      }
      if (signal.signal_state === "red" && Number.isInteger(signal.selected_detection_index)) {
        return { label: "보행자 신호", title: "빨간불입니다.", copy: "횡단보도 앞에서 기다려 주세요.", icon: "hand", tone: "signal" };
      }
      if (surface.voice_text && (surface.repeat || /^outside_/.test(surface.status || ""))) {
        return { label: "보행로 안내", title: surface.voice_text, copy: "주변을 확인하며 보행로 안으로 이동해 주세요.", icon: "alert", tone: "walk" };
      }
      if (walking.level === "danger" && walkingText) {
        return { label: "보행 장애물", title: walkingText, copy: "이동 방향과 주변을 확인해 주세요.", icon: "alert", tone: "walk" };
      }
      if (!BUS_SCREENS.has(screen) && signal.signal_state === "green" && Number.isInteger(signal.selected_detection_index)) {
        return { label: "보행자 신호", title: "초록불입니다.", copy: "음성 안내와 주변 상황을 함께 확인해 주세요.", icon: "walk", tone: "signal" };
      }
      return null;
    }
    function busCue() {
      if (!BUS_SCREENS.has(screen) || bus.route !== readRoute()) return null;
      if (bus.ocrStatus === "error") return { label: "카메라 번호 인식", title: "번호 인식을 사용할 수 없어요.",
        copy: bus.ocrMessage, icon: "alert", tone: "search" };
      if (bus.ocrStatus === "loading") return { label: "카메라 번호 인식", title: "번호 인식을 준비하고 있어요.",
        copy: bus.ocrMessage, icon: "scan", tone: "search" };
      const age = Date.now() - bus.ocrCapturedAt;
      if (!Number.isFinite(bus.ocrCapturedAt) || age < 0 || age > 3000) return null;
      if (!["candidate", "confirmed", "other"].includes(bus.ocrStatus)) return null;
      return { label: "카메라 번호 인식", title: bus.ocrMessage,
        copy: bus.ocrStatus === "other" ? "찾는 버스의 번호를 계속 확인합니다." : "버스 번호와 주변 상황을 함께 확인해 주세요.",
        icon: bus.ocrStatus === "confirmed" ? "check" : "bus",
        tone: bus.ocrStatus === "confirmed" ? "recognized" : "search" };
    }
    function render(result) {
      lastResult = result || null;
      if (!CAMERA_SCREENS.has(screen) || paused) return;
      currentCue = hazardCue(lastResult) || busCue() || DEFAULT_CUES[screen];
    }
    function setObstacleDetection(enabled) { obstacleDetection = enabled; render(lastResult); }
    function updateControls() {
      all("[data-action], [data-mic], [data-rate], [data-size], #route-form button").forEach(element => {
        const action = element.dataset.action;
        const canCancel = ["end", "back", "cancel-end", "confirm-end", "save-error-log", "save-local-clip"].includes(action);
        element.disabled = (busy && !canCancel) || (paused && ["manual-arrival", "locate", "open-keypad"].includes(action)) ||
          (paused && (element.dataset.mic || element.type === "submit"));
      });
      $("bus-stations").disabled = busy || paused || stationCount === 0;
      $("record-button").disabled = busy || !clipState.available;
      $("app").setAttribute("aria-busy", String(busy));
    }
    function setBusy(value) { busy = Boolean(value); updateControls(); }
    function setLocalClips(records = []) {
      const panel = $("local-clips-panel"), select = $("local-clip-choice"), label = $("local-clips-status");
      if (!panel || !select || !label) return;
      const previous = select.value, fragment = document.createDocumentFragment();
      for (const record of records) {
        const option = document.createElement("option");
        option.value = record.key;
        const when = new Date(record.clip.started_at_ms).toLocaleString("ko-KR");
        option.textContent = `${when} · 구간 ${record.clip.index}`;
        fragment.appendChild(option);
      }
      select.replaceChildren(fragment);
      if (records.some(record => record.key === previous)) select.value = previous;
      panel.hidden = records.length === 0;
      label.textContent = records.some(record => !record.durable)
        ? "기기 보관 실패 · 페이지를 닫기 전에 영상을 저장해 주세요."
        : "서버 업로드 전 원본입니다. 서버 저장이 완료되면 자동으로 지워집니다.";
    }
    function setClipState(value) {
      clipState = { ...clipState, ...value };
      const button = $("record-button");
      button.setAttribute("aria-pressed", String(clipState.active));
      updateText($("record-label"), clipState.active ? "영상 구간 기록 끝내기" : "30초 영상 구간 기록");
      const message = clipState.active
        ? `${clipState.count}/${clipState.max}번째 구간 기록 중 · 최대 30초`
        : clipState.count >= clipState.max
          ? `${clipState.max}개 기록 완료 · 테스트를 종료하고 저장합니다`
          : clipState.count
            ? `${clipState.count}/${clipState.max}개 기록됨 · 이어서 기록할 수 있습니다`
            : `최대 30초씩 ${clipState.max}개 · ${clipState.max}개를 채우면 자동 종료 후 저장`;
      updateText($("clip-status"), message);
      const badge = $("test-panel-badge");
      if (badge) {
        badge.dataset.state = clipState.active ? "recording" : "idle";
        updateText(badge, clipState.active ? `● 녹화 중 ${clipState.count}/${clipState.max}`
          : `영상 ${clipState.count}/${clipState.max}`);
      }
      updateControls();
    }
    function setPaused(value) {
      paused = Boolean(value);
      if (paused) speech.stop();
      cameraPanel.classList.toggle("is-paused", paused);
      $("pause-card").hidden = !paused;
      $("bus-target").hidden = paused || !BUS_SCREENS.has(screen);
      $("bus-details").hidden = paused || !BUS_SCREENS.has(screen);
      $("pause-button").setAttribute("aria-pressed", String(paused));
      $("pause-icon").setAttribute("href", paused ? "#icon-play" : "#icon-pause");
      updateText($("pause-label"), paused ? "재개" : "일시중지");
      updateControls();
      if (!paused) render(lastResult);
    }
    function show(next, { focus = true } = {}) {
      if (!SCREENS.has(next)) throw new Error(`지원하지 않는 화면: ${next}`);
      const previous = screen;
      screen = next;
      if (previous !== next) { speech.stop(); keypad?.close({ restoreFocus: false }); }
      const panelName = CAMERA_SCREENS.has(screen) ? "camera" : screen;
      all("[data-panel]").forEach(panel => { panel.hidden = panel.dataset.panel !== panelName; });
      $("app").dataset.screen = screen;
      if (screen === "welcome" && previous !== "welcome") film.restart();
      if (film.setScene) film.setScene(screen);
      else film.setVisible(screen === "welcome");
      const busScreen = BUS_SCREENS.has(screen);
      updateText($("camera-title-text"), busScreen ? " 버스 인식 중" : "이동 안내 중");
      $("camera-route").hidden = !busScreen;
      $("camera-title").classList.toggle("bus-title", busScreen);
      $("bus-target").hidden = !busScreen || paused;
      $("bus-details").hidden = !busScreen || paused;
      $("stage-button").dataset.action = busScreen ? "repeat" : "manual-arrival";
      updateText($("stage-label"), busScreen ? "다시 듣기" : "버스 번호 입력");
      $("camera-left").dataset.action = busScreen ? "back" : "settings-type";
      updateText($("camera-left-label"), busScreen ? "이전 화면으로" : "글자 크기 설정");
      if (screen === "input") routeError("");
      render(lastResult);
      updateControls();
      // Automatic OCR/GPS updates must not move keyboard or screen reader focus.
      if (focus && previous !== screen && !(CAMERA_SCREENS.has(previous) && CAMERA_SCREENS.has(screen))) {
        window.scrollTo?.({ top: 0, behavior: "instant" });
        document.querySelector(`[data-panel="${panelName}"] .top-controls button:not(:disabled)`)?.focus({ preventScroll: true });
        announce(getGuidance());
      }
    }
    function setBus(value = {}) {
      bus = { ...bus, ...value };
      if (Object.prototype.hasOwnProperty.call(value, "route")) setRoute(value.route);
      updateText($("bus-station-name"), String(bus.station || "정류장 확인 중"));
      updateText($("bus-arrival-text"), String(bus.arrivalText || "도착정보를 확인하고 있어요."));
      for (const [key, id] of [["message", "bus-message"], ["gpsMessage", "bus-gps-message"], ["ocrMessage", "bus-ocr-message"]]) {
        updateText($(id), String(bus[key] || ""));
        $(id).hidden = !bus[key] || (key === "message" && [bus.gpsMessage, bus.ocrMessage].includes(bus[key]));
      }
      if (bus.status) $("bus-target").dataset.status = String(bus.status);
      render(lastResult);
    }
    function setStations(stations = [], selectedId = null) {
      const select = $("bus-stations"), previous = select.value;
      const fragment = document.createDocumentFragment();
      stationCount = stations.length;
      if (!stations.length) {
        const option = document.createElement("option");
        option.value = ""; option.textContent = "가까운 정류장 확인 중";
        fragment.appendChild(option);
      }
      for (const station of stations) {
        const option = document.createElement("option");
        option.value = String(station.key ?? station.id ?? station.station_id ?? station.node_id ?? "");
        const distance = Number.isFinite(station.distance_m) ? ` · ${Math.round(station.distance_m)}m` : "";
        const direction = station.direction ? ` · ${station.direction}` : "";
        option.textContent = `${station.name || station.station_name || station.node_name || "정류장"}${direction}${distance}`;
        fragment.appendChild(option);
      }
      select.replaceChildren(fragment);
      const selected = selectedId === null ? previous : String(selectedId);
      if ([...select.options].some(option => option.value === selected)) select.value = selected;
      $("station-picker").hidden = stationCount === 0;
      updateControls();
    }
    function setSettings({ rate, textScale } = {}) {
      if (Number.isFinite(rate)) {
        all("[data-rate]").forEach(button => button.setAttribute("aria-pressed", String(Number(button.dataset.rate) === rate)));
      }
      if (Number.isFinite(textScale)) {
        const value = textScale === 2 ? 1.5 : [1, 1.2, 1.5].includes(textScale) ? textScale : 1;
        all("[data-size]").forEach(button => button.setAttribute("aria-pressed", String(Number(button.dataset.size) === value)));
        $("app").style.setProperty("--text-scale", String(value));
      }
    }
    function getGuidance() {
      if (paused) return "안내를 잠시 멈췄어요. 촬영과 자동 안내가 중지됩니다.";
      if (screen === "input") return "버스 번호를 입력하거나 음성 입력을 눌러 말해 주세요.";
      if (screen === "home") return "음성 속도 설정";
      if (screen === "type") return "글자 크기 설정";
      if (screen === "end") return "안내를 종료할까요?";
      if (screen === "welcome") return "카메라 안내로, 버스에 오르기까지. 길동무 시작하기를 눌러 주세요.";
      render(lastResult);
      return [currentCue.title, currentCue.copy].filter(Boolean).join(" ");
    }
    function speak(message) { announce(message); invoke(onSpeak, message); }
    function submitRoute(value) {
      if (busy || paused || screen !== "input") return;
      const route = window.GRouteKeypad.normalize(value);
      if (!route) { routeError("번호 다시 입력"); speak("번호 다시 입력"); return; }
      speech.stop();
      routeError(""); setRoute(route);
      invoke(onSubmitRoute, route);
    }
    const speech = window.GSpeechInput.create({
      onState(active, mode) {
        if (active) micMode = mode;
        all("[data-mic]").forEach(button => {
          const selected = active && button.dataset.mic === micMode;
          button.setAttribute("aria-pressed", String(selected));
          button.setAttribute("aria-label", selected ? "음성 입력 취소" : "버스 번호 음성 입력");
        });
        updateText($("mic-label"), active ? "듣는 중" : "음성 입력");
        if (active) {
          updateText($("route-hint"), "음성 입력 중 · 탈 버스 번호를 말해 주세요.");
          announce($("route-hint").textContent);
        }
        if (!active && speechSession) {
          speechSession = false;
          invoke(onAction, "speech-end");
        }
      },
      onResult({ mode, value }) {
        if (destroyed || busy || paused) return;
        if (mode === "input" && screen === "input") submitRoute(value);
      },
      onError(message) { speech.stop(); routeError(message); speak(message); },
    });
    keypad = window.GRouteKeypad.create({
      onChange(value) { setRoute(value, { syncKeypad: false }); routeError(""); },
      onSubmit: submitRoute,
      onSpeak: speak,
      onOpenChange(open) { if (open) speech.stop(); },
    });
    if (!speech.supported) updateText($("route-hint"), "음성 입력을 지원하지 않는 브라우저입니다. 번호 입력 버튼을 눌러 주세요.");
    function takeAction() {
      const now = Date.now();
      if (now - lastActionAt < 250) return false;
      lastActionAt = now; return true;
    }
    all("[data-action]").forEach(button => bind(button, "click", () => {
      if (button.disabled || !takeAction()) return;
      speech.stop();
      if (button.dataset.action === "open-keypad") {
        if (!busy && !paused && screen === "input") keypad.open(readRoute());
      } else invoke(onAction, button.dataset.action);
    }));
    all("[data-mic]").forEach(button => bind(button, "click", () => {
      if (busy || paused || button.disabled || !takeAction()) return;
      if (speech.isActive()) { speech.stop(); return; }
      routeError("");
      micMode = button.dataset.mic;
      if (speech.start(micMode)) {
        speechSession = true;
        invoke(onAction, "speech-start");
      }
    }));
    bind($("route-form"), "submit", event => {
      event.preventDefault();
      if (takeAction()) submitRoute(readRoute());
    });
    all("[data-rate]").forEach(button => bind(button, "click", () => {
      if (busy) return;
      const rate = Number(button.dataset.rate);
      setSettings({ rate }); invoke(onRateChange, rate); announce(`${rate}배`);
    }));
    all("[data-size]").forEach(button => bind(button, "click", () => {
      if (busy) return;
      const textScale = Number(button.dataset.size);
      setSettings({ textScale }); invoke(onTextScaleChange, textScale);
      speak(`글자 크기, ${button.textContent.trim()}`);
    }));
    bind($("bus-stations"), "change", event => invoke(onStationChange, event.target.value));
    bind(document, "visibilitychange", () => { if (document.hidden) { speech.stop(); keypad.close({ restoreFocus: false }); } });
    function destroy() {
      destroyed = true;
      speech.destroy();
      keypad.destroy();
      film.destroy();
      listeners.forEach(remove => remove());
    }
    show("welcome", { focus: false });
    return { show, render, setObstacleDetection, setBus, setStations, setPaused, setBusy, setClipState, setStatus, setRoute, readRoute, setRouteError: routeError,
      setSettings, setLocalClips, readLocalClipKey: () => $("local-clip-choice")?.value,
      announce, getScreen: () => screen, getGuidance, destroy };
  }
  window.GView = { create };
})();
