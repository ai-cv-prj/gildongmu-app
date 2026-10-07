const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function harness({ stopFailsOnce = false, clipState = "ready", uploadResponseLostOnce = false, deferRouteSubmit = false, storedPreferences = null } = {}) {
  let now = 100, nextTimer = 0, screen = "home", cameraActive = false, cameraEnded;
  let cameraExposureChanged, busCameraMode = false;
  let handlers, voice = null, busState = { status: "idle", revision: 0, arrival_event_id: null }, routeInput = "";
  let journeyCallbacks, journeyPaused = false, route = null;
  let sessionsStarted = 0, sessionsStopped = 0, stopAttempts = 0, uploadAttempts = 0;
  let recording = false, recordingStartedAt = null, recorderEnded = null;
  let releaseRouteSubmit = null, configuredPreferences = null, savedPreferences = null;
  const routeSubmissions = [], routeErrors = [], shownScreens = [];
  let recordStarts = 0, recordStops = 0;
  const timeline = [], uploads = [], diagnostics = [];
  let diagnosticContext = () => ({});
  const timers = new Map(), intervals = new Map(), frames = [], guides = [], journeyStarts = [], renders = [], statuses = [];
  const captures = [], startModes = [], cameraModeCalls = [], overlayRenders = [];
  const startSettings = [], metadataUpdates = [];
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) {
      const listeners = new Map();
      elements.set(id, { value: id === "device" ? "test phone" : "", style: {},
        classList: { toggle() {} },
        addEventListener(type, callback) {
          if (!listeners.has(type)) listeners.set(type, []);
          listeners.get(type).push(callback);
        },
        dispatchEvent(event) { for (const callback of listeners.get(event.type) || []) callback(event); },
      });
    }
    return elements.get(id);
  };
  const view = { setSettings(value) { configuredPreferences = value; }, show(value) { screen = value; shownScreens.push(value); }, getScreen: () => screen,
    setObstacleDetection() {}, setBusy() {}, setStatus(text) { statuses.push(text); }, announce() {}, setRoute(value) { routeInput = value; }, setBus() {},
    readRoute: () => routeInput, setRouteError(message) { routeErrors.push(message); },
    setStations() {}, setPaused() {}, render(value) { renders.push(value); }, getGuidance: () => "안내" };
  const player = { setRate: value => value, recordingStream: () => null, cancel() { voice = null; },
    speak(text, validUntil, callbacks) { voice = { text, validUntil, ...callbacks }; callbacks.onStart?.(); return true; } };
  const settings = { audio: { tick_ms: 250, playback_timeout_ms: 15000, crosswalk_max_age_ms: 1500 },
    camera: { capture_max_side: 640, jpeg_quality: .72, bus_capture_max_side: 960,
      bus_jpeg_quality: .72, bus_capture_interval_ms: 300 } };
  const journey = { start(value) {
    route = value; journeyStarts.push(value); journeyPaused = false;
    journeyCallbacks.onChange({ route, gps: { status: "locating", candidates: [] }, ocr: {} });
  }, pause() { journeyPaused = true; }, resume() { if (route) journeyPaused = false; },
    stop() { route = null; journeyPaused = false; }, accept() {}, selectStop() {}, repeat() {},
    snapshot() { return { ocr: { status: "searching", routeNumber: null } }; } };
  const api = { isRetryable: error => error?.retryable === true || (!error?.status && error?.name === "TypeError"),
    start: async (device, note, busHighres) => {
    startSettings.push({ device_name: device, note });
    sessionsStarted++; startModes.push(busHighres); return { session_id: "session-1" }; },
    updateMetadata: async (id, device, note) => {
      const saved = { session_id: id, device_name: device, note };
      metadataUpdates.push(saved); return saved;
    },
    stop: async () => {
      stopAttempts++;
      if (stopFailsOnce && stopAttempts === 1) throw Object.assign(new Error("일시적 연결 실패"), { status: 503 });
      sessionsStopped++; timeline.push("stop"); return { frame_count: frames.length };
    },
    frame(id, frameId, capturedAtMs, blob, signal, busBlob, busCapturedAtMs) {
      return new Promise((resolve, reject) => {
        const abort = () => reject(Object.assign(new Error("요청 취소"), { name: "AbortError" }));
        if (signal?.aborted) abort();
        else signal?.addEventListener("abort", abort, { once: true });
        frames.push({ id, frameId, capturedAtMs, blob, signal, busBlob, busCapturedAtMs,
          resolve(value) { signal?.removeEventListener("abort", abort); resolve(value); },
          reject(error) { signal?.removeEventListener("abort", abort); reject(error); } });
      });
    },
    async boarding(_id, action, _event, number) {
      if (action === "submit") {
        routeSubmissions.push(number);
        if (deferRouteSubmit) await new Promise(resolve => { releaseRouteSubmit = resolve; });
      }
      if (action === "arrive") busState = { status: "awaiting_stop", arrival_event_id: (busState.arrival_event_id ?? 0) + 1,
        revision: busState.revision + 1, arrival_source: "user_confirmed", bus_number: null };
      else {
        const state = { stop_announced: "pending", submit: "submitted", reopen: "awaiting_stop", cancel: "cancelled" }[action];
        busState = { ...busState, revision: busState.revision + 1, status: state,
          bus_number: action === "submit" ? number : null };
      }
      return { ...busState };
    },
    timings: async () => ({}), heartbeat: async () => ({}), busEvents: async () => ({}), recordingEvent: async () => ({}),
    recording: async () => ({}), camera: async () => ({}),
    clips: async () => ({ clips: uploads.map(clip => ({ clip_id: clip.index, state: clipState,
      error: clipState === "failed" ? "인코딩 실패" : null })) }),
    uploadClip: async (_id, clip) => { timeline.push(`upload:${clip.index}`); uploads.push(clip); uploadAttempts++;
      if (uploadResponseLostOnce && uploadAttempts === 1) throw new Error("완료 응답을 받지 못했습니다.");
      return { state: "pending" }; },
  };
  const context = {
    console, AbortController, Date: { now: () => 100000 + now },
    performance: { now: () => now, timeOrigin: 100000 },
    localStorage: { getItem: () => storedPreferences && JSON.stringify(storedPreferences),
      setItem(_key, value) { savedPreferences = JSON.parse(value); } },
    document: { hidden: false, getElementById: node, addEventListener() {} },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { const id = ++nextTimer; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    GView: { create(options) { handlers = options; return view; } },
    GDiagnostics: { record(type, fields = {}) { diagnostics.push({ ...diagnosticContext(), type, ...fields }); },
      setContext(provider) { diagnosticContext = provider; }, flush: async () => {} },
    GTts: { create: () => player }, GApi: api, GConfig: { get: () => settings, load: async () => settings },
    GGuidance: { create() { const guide = { accepted: [], starts: 0, stops: 0, start() { this.starts++; }, stop() { this.stops++; }, tick() {},
      accept(value) { this.accepted.push(value); } }; guides.push(guide); return guide; } },
    GBusJourney: { create(options) { journeyCallbacks = options; return journey; } },
    GCamera: { video: { videoWidth: 1280, videoHeight: 720 },
      async start() { cameraActive = true; busCameraMode = false; return { width: 1280, height: 720 }; },
      stop() { cameraActive = false; busCameraMode = false; }, active: () => cameraActive,
      async setBusMode(enabled) { cameraModeCalls.push(enabled); busCameraMode = enabled;
        const state = { status: enabled ? "applied" : "default", requested: enabled,
          target_us: 16667, actual_us: enabled ? 16670 : null, settings: {} };
        cameraExposureChanged?.(state); return state; },
      setOnExposureChange(callback) { cameraExposureChanged = callback; },
      capture: async maxSide => { captures.push(maxSide); return { size: 12, maxSide }; },
      pause() {}, resume() {}, setOnEnded(callback) { cameraEnded = callback; } },
    GRecorder: { startPreview() {}, stopPreview() {}, pause() {}, resume() {}, bytes: () => 0,
      startRaw(_onChunk, onEnded) { recording = true; recordStarts++; recorderEnded = onEnded;
        recordingStartedAt = 100000 + now; return recordingStartedAt; },
      async stopRaw() { if (!recording) return null; recording = false; recordStops++;
        return { blob: { size: 12, type: "video/webm" }, started_at_ms: recordingStartedAt,
          ended_at_ms: Math.min(100000 + now, recordingStartedAt + 30000) }; } },
    GOverlay: { size() {}, clear() {}, snapshot: () => "cG5n",
      render(result, done) { overlayRenders.push(result); done("drawn"); } },
    fetch: async () => ({}), addEventListener() {},
    queueMicrotask,
  };
  context.window = context;
  vm.createContext(context);
  for (const name of ["audio_coordinator", "boarding"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  await vm.runInContext(fs.readFileSync("frontend/js/app.js", "utf8"), context);
  const action = async value => { await handlers.onAction(value); await flush(); };
  const frameResult = frame => ({ session_id: "session-1", frame_id: frame.frameId,
    captured_at_ms: frame.capturedAtMs, inference_ms: 10,
    walking: { detections: [], event: {} }, traffic: { detections: [], event: {} },
    boarding: { ...busState }, stop_proximity: { nearby: false } });
  return { api, node, startSettings, metadataUpdates,
    async event(id, type, value) {
      if (value !== undefined) node(id).value = value;
      node(id).dispatchEvent({ type, target: node(id) }); await flush();
    },
    diagnostics, action, frames, captures, startModes, guides, journeyStarts, renders, statuses, uploads, timeline, routeSubmissions, overlayRenders,
    routeErrors, shownScreens, typeRoute(value) { routeInput = value; },
    async end() { await action("end"); await action("confirm-end"); },
    submit: value => handlers.onSubmitRoute(value),
    releaseSubmit() { releaseRouteSubmit?.(); },
    async gps(state) { journeyCallbacks.onChange({ route, gps: { status: "active", selected: {
      arrival: { first_arrival_state: state }, station: { station_name: "테스트 정류장" } }, candidates: [] }, ocr: {} }); await flush(); },
    preferences: () => ({ configured: configuredPreferences, saved: savedPreferences }),
    rate(value) { handlers.onRateChange(value); },
    async previewTimer(delay) { const entry = [...timers].find(([, value]) => value.delay === delay);
      if (!entry) return false; const [id, value] = entry; timers.delete(id); value.callback(); await flush(); return true; },
    voice: () => voice,
    cameraModeCalls, busCameraMode: () => busCameraMode,
    exposureCallbackRegistered: () => typeof cameraExposureChanged === "function",
    recordCounts: () => ({ starts: recordStarts, stops: recordStops, recording }), screen: () => screen,
    retryVisible: () => !node("retry-upload").hidden,
    resources: () => ({ cameraActive, sessionsStarted, sessionsStopped, timers: timers.size, intervals: intervals.size, route }),
    journeyPaused: () => journeyPaused, routeInput: () => routeInput, setNow(value) { now = value; },
    async runTimer(delay) {
      const entry = [...timers].find(([, value]) => value.delay === delay);
      if (!entry) return null;
      const [id, timer] = entry; timers.delete(id); now += delay;
      const pending = timer.callback(); await flush(); return { pending };
    },
    async capture() {
      const [id, timer] = [...timers].find(([, value]) => value.delay === 0);
      timers.delete(id);
      const callback = timer.callback;
      const pending = callback(); await flush(); return { pending };
    },
    async autoStopClip() {
      const [id, timer] = [...timers].find(([, value]) => value.delay === 29750);
      timers.delete(id); now += 29750; timer.callback(); await flush();
    },
    async respond(index, overrides = {}) { frames[index].resolve({ ...frameResult(frames[index]), ...overrides }); await flush(); },
    async finishVoice() { const done = voice; voice = null; done.onEnd(); await flush(); },
    async draftRoute(value) { await handlers.onSubmitRoute(value); await flush(); },
    async confirmRoute(value) { await handlers.onSubmitRoute(value); await flush(); },
    async cameraEnd() { cameraActive = false; cameraEnded(); await flush(); },
    async recorderError() { recording = false; recordStops++; recorderEnded?.(new Error("인코더 오류")); await flush(); },
  };
}

test("수동 도착은 멈춤 안내 중 번호 입력을 허용하고 완료 후에만 버스 찾기와 노출 설정을 시작한다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  assert.equal(app.screen(), "input", "멈춤 안내와 동시에 번호 입력 화면을 연다");
  app.typeRoute("143");
  await app.confirmRoute("143");
  assert.equal(app.screen(), "input");
  assert.match(app.routeErrors.at(-1), /멈춤 안내가 끝나면/);
  assert.deepEqual(app.routeSubmissions, []);
  assert.deepEqual(app.cameraModeCalls, []);
  assert.deepEqual(app.journeyStarts, [], "멈춤 안내 중에는 버스 찾기를 시작하지 않는다");
  const shownBeforeStop = app.shownScreens.length;
  await app.finishVoice();
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "143", "멈춤 안내 전에 입력한 번호를 보존한다");
  assert.equal(app.shownScreens.length, shownBeforeStop, "음성 완료로 화면을 다시 열거나 키패드를 닫지 않는다");
  assert.equal(app.routeErrors.at(-1), "");
  await app.action("confirm-route");
  assert.equal(app.screen(), "search");
  assert.deepEqual(app.journeyStarts, ["143"]);
  assert.deepEqual(app.cameraModeCalls, [true]);
  await app.end();
});

test("멈춤 안내 중 제출하지 않은 번호도 설정이나 종료 확인을 다녀와서 이어 입력한다", async () => {
  for (const destination of ["settings-type", "end"]) {
    const app = await harness();
    await app.action("start"); await app.action("manual-arrival");
    app.typeRoute("N26");
    await app.action(destination);
    const temporary = app.screen();
    await app.finishVoice();
    assert.equal(app.screen(), temporary);
    await app.action(destination === "end" ? "cancel-end" : "start");
    assert.equal(app.screen(), "input");
    assert.equal(app.routeInput(), "N26");
    assert.deepEqual(app.routeSubmissions, []);
    await app.confirmRoute(app.routeInput());
    assert.deepEqual(app.journeyStarts, ["N26"]);
    await app.end();
  }
});

test("멈춤 안내가 아직 끝나지 않아도 설정과 종료 확인에서 번호 입력으로 돌아간다", async () => {
  for (const destination of ["settings-type", "end"]) {
    const app = await harness();
    await app.action("start"); await app.action("manual-arrival");
    app.typeRoute("606");
    await app.action(destination);
    await app.action(destination === "end" ? "cancel-end" : "start");
    assert.equal(app.screen(), "input");
    assert.equal(app.routeInput(), "606");
    await app.confirmRoute("606");
    assert.deepEqual(app.routeSubmissions, []);
    await app.finishVoice();
    await app.confirmRoute("606");
    assert.deepEqual(app.journeyStarts, ["606"]);
    await app.end();
  }
});

test("LED 노출 설정은 노선 확정 때 적용하고 이전·수정 시 복원한다", async () => {
  const app = await harness();
  assert.equal(app.exposureCallbackRegistered(), true);
  await app.action("start");
  assert.equal(app.busCameraMode(), false);
  assert.deepEqual(app.cameraModeCalls, [], "처음 보행 안내는 기본 카메라 설정을 사용한다");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.draftRoute("잘못된 번호");
  assert.deepEqual(app.cameraModeCalls, [], "정류장 도착과 유효하지 않은 번호만으로는 설정을 바꾸지 않는다");
  await app.draftRoute("143");
  assert.deepEqual(app.cameraModeCalls, [true]);
  assert.equal(app.busCameraMode(), true);
  const frame = await app.capture();
  await app.respond(0); await frame.pending;
  assert.deepEqual(app.cameraModeCalls, [true], "동일한 submitted 상태가 재통지되어도 재적용하지 않는다");

  await app.action("back");
  assert.deepEqual(app.cameraModeCalls, [true, false]);
  assert.equal(app.busCameraMode(), false);
  assert.equal(app.resources().cameraActive, true, "설정 복원 중에도 공유 카메라 세션은 유지한다");
  await app.finishVoice();
  await app.confirmRoute("606");
  assert.deepEqual(app.cameraModeCalls, [true, false, true]);
  await app.action("edit-route");
  assert.deepEqual(app.cameraModeCalls, [true, false, true, false]);
  assert.equal(app.busCameraMode(), false);
  await app.end();
});

test("버스 노선 제출 후에도 보행 640px을 유지하고 버스 960px을 간격에 맞춰 전송한다", async () => {
  const app = await harness();
  await app.action("start");
  assert.deepEqual(app.startModes, [true]);
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  const first = await app.capture();
  assert.deepEqual(app.captures, [640, 960]);
  assert.equal(app.frames[0].blob.maxSide, 640);
  assert.equal(app.frames[0].busBlob.maxSide, 960);
  assert.equal(app.frames[0].busCapturedAtMs, app.frames[0].capturedAtMs);
  await app.respond(0); await first.pending;
  assert.equal(app.overlayRenders[0].bus_mode, true);
  assert.equal(app.overlayRenders[0].target_route, "143");
  assert.equal(app.overlayRenders[0].bus_guidance.status, "searching");
  app.setNow(250);
  const second = await app.capture();
  assert.deepEqual(app.captures, [640, 960, 640]);
  assert.equal(app.frames[1].busBlob, null);
  await app.respond(1); await second.pending;
  app.setNow(500);
  const third = await app.capture();
  assert.deepEqual(app.captures, [640, 960, 640, 640, 960]);
  assert.equal(app.frames[2].busBlob.maxSide, 960);
  await app.respond(2); await third.pending;
  await app.end();
});

test("시작 화면에서 출발 준비로 이동할 때 카메라나 추론 세션을 시작하지 않는다", async () => {
  const app = await harness();
  assert.equal(app.screen(), "welcome");
  await app.action("enter");
  assert.equal(app.screen(), "home");
  assert.deepEqual(app.resources(), { cameraActive: false, sessionsStarted: 0, sessionsStopped: 0,
    timers: 0, intervals: 0, route: null });
  await app.action("start");
  assert.equal(app.screen(), "walk");
  assert.equal(app.resources().sessionsStarted, 1);
  await app.end();
});

test("종료 확인 후에만 카메라와 세션을 정리하고 시작 화면으로 돌아간다", async () => {
  const app = await harness();
  await app.action("enter");
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  await app.action("end");
  assert.equal(app.screen(), "end");
  assert.equal(app.resources().cameraActive, true);
  assert.equal(app.resources().sessionsStopped, 0);
  await app.action("confirm-end");
  assert.equal(app.screen(), "welcome");
  assert.deepEqual(app.resources(), { cameraActive: false, sessionsStarted: 1, sessionsStopped: 1,
    timers: 0, intervals: 0, route: null });
  assert.equal(app.screen(), "welcome");
  assert.equal(app.resources().cameraActive, false);
  await app.action("enter");
  await app.action("start");
  assert.equal(app.screen(), "walk");
  assert.equal(app.resources().sessionsStarted, 2);
  await app.end();
});

test("이동 안내에서 이전을 누르면 마무리 대신 출발 준비로 돌아간다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("back");
  assert.equal(app.screen(), "home");
  assert.equal(app.resources().cameraActive, false);
  assert.equal(app.resources().sessionsStopped, 1);
});

test("일시중지 중에도 이전을 누르면 출발 준비로 돌아간다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("pause");
  await app.action("back");
  assert.equal(app.screen(), "home");
  assert.equal(app.resources().cameraActive, false);
  assert.equal(app.resources().sessionsStopped, 1);
});

test("버스 노선 안내를 일시중지한 뒤 이전을 누르면 재개하고 노선 입력으로 돌아간다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  await app.action("pause");
  assert.equal(app.journeyPaused(), true);
  assert.deepEqual(app.cameraModeCalls, [true], "일시중지는 활성 노선의 설정을 유지한다");
  await app.action("back");
  assert.deepEqual(app.cameraModeCalls, [true, false], "일시중지 후 이전도 노선 해제와 함께 복원한다");
  assert.equal(app.busCameraMode(), false);
  assert.equal(app.resources().route, null, "이전이 무시되지 않고 노선 안내를 해제한다");
  assert.equal(app.journeyPaused(), false);
  assert.equal(app.resources().cameraActive, true, "노선을 다시 고르는 동안 세션을 유지한다");
  assert.equal(app.resources().sessionsStopped, 0);
  await app.end();
});

test("새 정류장 도착에서는 이전 도착 때 입력한 버스 번호를 지운다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.draftRoute("143");
  assert.equal(app.screen(), "search");
  await app.action("back");
  await app.finishVoice();
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "143", "번호를 다시 고르면 입력한 번호를 유지한다");
  await app.action("back");
  assert.equal(app.screen(), "walk");
  await app.action("manual-arrival");
  await app.finishVoice();
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "", "새 도착에서는 입력란을 비운다");
  await app.end();
});

test("빠른 일시중지·재개 중 응답한 이전 프레임은 순번만 반영하고 안내에 재사용하지 않는다", async () => {
  const app = await harness();
  await app.action("start");
  const { pending } = await app.capture();
  assert.equal(app.frames[0].frameId, 1);
  await app.action("pause");
  app.setNow(200);
  await app.action("pause");
  await app.respond(0);
  await pending;
  assert.equal(app.guides[0].accepted.length, 0, "지난 프레임은 보행 안내에 사용하지 않는다");
  assert.equal(app.guides[1].accepted.length, 0, "지난 프레임은 신호 안내에 사용하지 않는다");
  const next = await app.capture();
  assert.equal(app.frames[1].frameId, 2, "서버에 제출한 프레임 순번은 계속 증가한다");
  await app.respond(1);
  await next.pending;
  assert.equal(app.guides[0].accepted.length, 1);
  await app.end();
});

test("카메라 종료 후 GPS만 계속할 때도 일시중지 버튼으로 GPS 안내를 멈출 수 있다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  assert.equal(app.exposureCallbackRegistered(), true, "노출 상태 콜백과 트랙 종료 콜백을 함께 등록한다");
  await app.cameraEnd();
  assert.equal(app.resources().cameraActive, false);
  assert.match(app.statuses.at(-1), /카메라가 종료되었습니다/);
  assert.equal(app.journeyPaused(), false, "카메라 종료와 GPS 안내는 독립적이다");
  await app.action("pause");
  assert.equal(app.journeyPaused(), true, "사용자 일시중지는 GPS도 멈춘다");
  await app.end();
});

test("녹화는 기본 꺼짐이며 선택한 두 구간을 세션 종료 후 각각 업로드한다", async () => {
  const app = await harness();
  await app.action("start");
  assert.deepEqual(app.recordCounts(), { starts: 0, stops: 0, recording: false });

  await app.action("record");
  const first = await app.capture();
  await app.respond(0); await first.pending;
  app.setNow(1100);
  await app.action("record");

  await app.action("record");
  const second = await app.capture();
  await app.respond(1); await second.pending;
  app.setNow(2200);
  await app.action("record");
  assert.deepEqual(app.recordCounts(), { starts: 2, stops: 2, recording: false });
  assert.equal(app.uploads.length, 0, "추론 중에는 영상을 전송하지 않는다");

  await app.end();
  assert.deepEqual(app.timeline, ["stop", "upload:1", "upload:2"]);
  assert.deepEqual(app.uploads.map(clip => clip.index), [1, 2]);
  assert.deepEqual(JSON.parse(JSON.stringify(app.uploads.map(clip => clip.frames.map(frame => frame.frame_id)))),
    [[1], [2]]);
  assert.strictEqual(app.uploads[0].frames[0].image, app.frames[0].blob,
    "라이브 추론에 보낸 동일한 JPEG를 사후 업로드한다");
  assert.equal(app.uploads[0].frames[0].overlay_png, "cG5n",
    "화면에 실제 그린 오버레이도 같은 프레임과 함께 업로드한다");
  assert.ok(app.uploads.every(clip => clip.ended_at_ms - clip.started_at_ms <= 30000));
  assert.match(app.statuses.at(-1), /원본·추론 영상 저장이 완료/);
});

test("30초 자동 종료 뒤에도 새 구간을 기록할 수 있고 한 세션은 다섯 구간으로 제한된다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("record");
  await app.autoStopClip();
  assert.deepEqual(app.recordCounts(), { starts: 1, stops: 1, recording: false });
  for (let index = 1; index < 5; index++) {
    await app.action("record");
    app.setNow(30000 + index * 1000);
    await app.action("record");
  }
  await app.action("record");
  assert.deepEqual(app.recordCounts(), { starts: 5, stops: 5, recording: false });
  assert.match(app.statuses.at(-1), /최대 5개/);
  await app.end();
});

test("일시중지와 카메라 종료는 현재 영상 구간을 끝내며 재개 시 새 구간을 쓴다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("record");
  app.setNow(1100);
  await app.action("pause");
  assert.equal(app.recordCounts().recording, false);
  await app.action("pause");
  await app.action("record");
  app.setNow(2200);
  await app.cameraEnd();
  assert.deepEqual(app.recordCounts(), { starts: 2, stops: 2, recording: false });
  await app.end();
});

test("영상이 없어도 종료 요청 실패를 화면에서 다시 시도할 수 있다", async () => {
  const app = await harness({ stopFailsOnce: true });
  await app.action("start");
  await app.end();
  assert.equal(app.resources().sessionsStopped, 0);
  assert.equal(app.retryVisible(), true);
  await app.action("retry-upload");
  assert.equal(app.resources().sessionsStopped, 1);
  assert.equal(app.retryVisible(), false);
});

test("영상 변환 실패가 업로드 성공으로 오인되지 않고 사용자에게 표시된다", async () => {
  const app = await harness({ clipState: "failed" });
  await app.action("start");
  await app.action("record");
  const frame = await app.capture();
  await app.respond(0); await frame.pending;
  app.setNow(1200);
  await app.action("record");
  await app.end();
  assert.equal(app.uploads.length, 1);
  assert.match(app.statuses.at(-1), /영상 결과 생성 실패/);
});

test("complete 응답 손실 뒤 재시도는 저장 상태를 조회하고 같은 청크를 다시 올리지 않는다", async () => {
  const app = await harness({ uploadResponseLostOnce: true });
  await app.action("start");
  await app.action("record");
  const frame = await app.capture();
  await app.respond(0); await frame.pending;
  app.setNow(1200);
  await app.action("record");
  await app.end();
  assert.equal(app.retryVisible(), true);
  await app.action("retry-upload");
  assert.deepEqual(app.timeline, ["stop", "upload:1"]);
  assert.equal(app.uploads.length, 1);
  assert.match(app.statuses.at(-1), /원본·추론 영상 저장이 완료/);
});

test("브라우저 녹화기가 자체 종료되면 현재 구간을 즉시 버리고 새 구간을 시작할 수 있다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("record");
  await app.recorderError();
  assert.equal(app.recordCounts().recording, false);
  await app.action("record");
  assert.equal(app.recordCounts().starts, 2, "고장난 구간을 계속 붙잡고 있지 않는다");
  app.setNow(1200);
  await app.action("record");
  await app.end();
});

test("정류장 도착 후 늦은 장애물 결과와 재개를 차단하고 취소하면 다시 안내한다", async () => {
  const app = await harness();
  await app.action("start");
  await app.capture();
  const walking = app.guides[0];
  const stops = walking.stops;
  await app.action("manual-arrival");
  assert.equal(walking.stops, stops + 1);
  await app.respond(0, { boarding: { status: "searching", revision: 0 },
    walking: { detections: [{ class_name: "person" }], event: { level: "danger", voice_text: "멈추세요" } } });
  assert.equal(walking.accepted.length, 0);
  assert.equal(app.renders.at(-1).walking.event.enabled, false);
  await app.finishVoice();
  await app.confirmRoute("143");
  const starts = walking.starts;
  await app.action("pause");
  await app.action("pause");
  assert.equal(walking.starts, starts);
  await app.action("back");
  await app.action("back");
  assert.equal(app.screen(), "walk");
  assert.equal(walking.starts, starts + 1);
  await app.capture();
  await app.respond(1);
  assert.equal(walking.accepted.length, 1);
  await app.end();
});


test("안내 중 설정을 오가도 세션을 유지하고 최신 버스 상태로 돌아간다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("settings-type");
  assert.equal(app.screen(), "type");
  await app.action("settings-home");
  assert.equal(app.screen(), "home");
  await app.action("start");
  assert.equal(app.screen(), "walk");
  assert.equal(app.resources().sessionsStarted, 1);
  await app.action("manual-arrival"); await app.finishVoice();
  await app.confirmRoute("143");
  await app.action("settings-type");
  await app.gps("arriving");
  assert.equal(app.screen(), "type", "GPS 갱신이 설정 화면을 닫지 않는다");
  await app.action("start");
  assert.equal(app.screen(), "approach");
  assert.equal(app.resources().sessionsStarted, 1);
  await app.end();
});

test("설정 또는 종료 확인 중 완료된 정류장 안내는 복귀 때 번호 입력으로 연결한다", async () => {
  for (const destination of ["settings-type", "end"]) {
    const app = await harness();
    await app.action("start");
    await app.action("manual-arrival");
    await app.action(destination);
    const screen = app.screen();
    await app.finishVoice();
    assert.equal(app.screen(), screen, "정류장 상태 갱신이 사용자가 연 화면을 덮지 않는다");
    await app.action(destination === "end" ? "cancel-end" : "start");
    assert.equal(app.screen(), "input");
    assert.equal(app.resources().sessionsStarted, 1);
    await app.end();
  }
});

test("종료 확인 중 GPS 갱신과 일시중지 상태를 유지하고 계속 안내로 복귀한다", async () => {
  const app = await harness();
  await app.action("start"); await app.action("manual-arrival"); await app.finishVoice();
  await app.confirmRoute("143"); await app.action("pause");
  await app.action("end");
  await app.gps("arrived");
  assert.equal(app.screen(), "end");
  assert.equal(app.journeyPaused(), true);
  await app.action("cancel-end");
  assert.equal(app.screen(), "arrived");
  assert.equal(app.journeyPaused(), true);
  assert.equal(app.resources().sessionsStopped, 0);
  await app.action("pause"); await app.end();
});

test("번호 입력은 한 번 제출하며 종료 뒤 완료된 제출 응답으로 안내를 재시작하지 않는다", async () => {
  const app = await harness({ deferRouteSubmit: true });
  await app.action("start");
  await app.submit("143");
  assert.deepEqual(app.routeSubmissions, [], "정류장 번호 입력 상태에서만 제출한다");
  await app.action("manual-arrival"); await app.finishVoice();
  const pending = app.submit(" n26번 ");
  await app.submit("271");
  assert.deepEqual(app.routeSubmissions, ["N26"]);
  await app.end();
  app.releaseSubmit(); await pending; await flush();
  assert.equal(app.screen(), "welcome");
  assert.deepEqual(app.journeyStarts, []);
  assert.equal(app.resources().sessionsStarted, 1);
  assert.equal(app.resources().sessionsStopped, 1);
});

test("글자 크기 최대 저장값을 이전 2배에서 새 시안 1.5배로 이어받는다", async () => {
  const app = await harness({ storedPreferences: { rate: 1.5, textScale: 2 } });
  assert.equal(app.preferences().configured.textScale, 1.5);
  assert.equal(app.preferences().configured.rate, 1.5);
  const previous = await harness({ storedPreferences: { rate: 1.25, textScale: 1.2 } });
  assert.equal(previous.preferences().configured.rate, 1, "이전 속도는 새 선택지 중 가까운 값으로 이어받는다");
});

test("음성 속도 선택은 두 번 미리 듣고 화면 이동이나 안내 시작 때 취소한다", async () => {
  const app = await harness();
  await app.action("enter"); app.rate(1.5);
  assert.equal(await app.previewTimer(220), true);
  assert.equal(app.voice().text, "왼쪽으로 한 걸음");
  await app.finishVoice();
  assert.equal(await app.previewTimer(360), true);
  assert.equal(app.voice().text, "왼쪽으로 한 걸음");
  await app.finishVoice();
  assert.equal(await app.previewTimer(360), false);
  app.rate(2); await app.action("settings-type");
  assert.equal(await app.previewTimer(220), false);
  await app.action("start"); await app.action("settings-home"); app.rate(1);
  assert.equal(await app.previewTimer(220), false, "이동 중에는 자동 안내를 속도 미리듣기로 끊지 않는다");
  await app.end();
});


test("추론 표시 전에 일시중지한 녹화 구간은 저장 대기를 남기지 않고 다시 안내를 시작한다", async () => {
  const app = await harness();
  await app.action("start"); await app.action("record");
  const frame = await app.capture();
  app.setNow(1100);
  await app.action("pause");
  await app.respond(0); await frame.pending;
  assert.equal(app.overlayRenders.length, 0, "일시중지 뒤 도착한 프레임은 화면에 표시하지 않는다");
  let ended = false;
  const ending = app.end().then(() => { ended = true; });
  // A missing server clip must not leave a phantom poll that blocks the next session.
  for (let attempt = 0; attempt < 16 && !ended; attempt++) {
    await flush(); await app.runTimer(2000);
  }
  assert.equal(ended, true);
  await ending;
  assert.equal(app.uploads.length, 0);
  assert.equal(app.retryVisible(), false);
  await app.action("start");
  assert.equal(app.resources().sessionsStarted, 2);
  assert.equal(app.screen(), "walk");
  await app.end();
});

test("번호 입력 중 일시적 프레임 연결 실패는 입력과 세션을 유지하며 같은 요청을 복구한다", async () => {
  const app = await harness();
  await app.action("start"); await app.action("manual-arrival"); await app.finishVoice();
  app.typeRoute("143");
  await app.action("record");
  const first = await app.capture();
  app.frames[0].reject(new TypeError("Failed to fetch"));
  await flush();
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "143");
  assert.equal(app.resources().cameraActive, true);
  assert.equal(app.resources().sessionsStopped, 0);
  assert.equal(app.recordCounts().recording, true, "일시적 실패로 녹화를 즉시 종료하지 않는다");
  assert.match(app.statuses.at(-1), /연결|재시도/);
  assert.ok(await app.runTimer(2000), "연결 복구 요청을 예약한다");
  assert.equal(app.frames.length, 2);
  for (const field of ["id", "frameId", "capturedAtMs", "blob", "busBlob", "busCapturedAtMs"])
    assert.strictEqual(app.frames[1][field], app.frames[0][field], field);
  assert.equal(app.captures.length, 1, "서버 처리 여부가 불확실한 프레임을 새 캡처로 바꾸지 않는다");
  assert.equal(app.recordCounts().recording, true);
  await app.respond(1); await first.pending;
  assert.equal(app.overlayRenders.length, 0, "복구된 과거 프레임을 현재 안내로 재생하지 않는다");
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "143");
  const next = await app.capture();
  assert.equal(app.frames[2].frameId, 2);
  await app.respond(2); await next.pending;
  assert.equal(app.overlayRenders.length, 1);
  assert.equal(app.resources().sessionsStarted, 1);
  app.setNow(3100);
  await app.end();
});

test("사용자 종료는 반복 재연결 대기와 진행 중 요청을 취소하고 이전 세션을 되살리지 않는다", async () => {
  for (const endDuringRequest of [false, true]) {
    const app = await harness();
    await app.action("start");
    const first = await app.capture();
    app.frames[0].reject(new TypeError("Failed to fetch")); await flush();
    assert.ok(await app.runTimer(2000));
    app.frames[1].reject(new TypeError("Failed to fetch")); await flush();
    if (endDuringRequest) assert.ok(await app.runTimer(2000));
    const attempts = app.frames.length;
    await app.end(); await first.pending;
    assert.equal(await app.runTimer(2000), null, "종료 뒤 재연결 타이머를 남기지 않는다");
    assert.equal(app.frames.length, attempts);
    assert.equal(app.resources().sessionsStopped, 1);
    assert.equal(app.resources().cameraActive, false);
    assert.equal(app.screen(), "welcome");
    if (endDuringRequest) {
      assert.equal(app.frames.at(-1).signal.aborted, true);
      await app.respond(attempts - 1);
      assert.equal(app.overlayRenders.length, 0);
      assert.equal(app.screen(), "welcome");
    }
    await app.action("start");
    assert.equal(app.resources().sessionsStarted, 2);
    await app.end();
  }
});

test("재연결 중 일시중지한 뒤 받은 응답은 프레임 순번만 복구하고 재개 뒤 새 영상으로 안내한다", async () => {
  const app = await harness();
  await app.action("start");
  const first = await app.capture();
  app.frames[0].reject(new TypeError("Failed to fetch")); await flush();
  assert.ok(await app.runTimer(2000));
  await app.action("pause");
  await app.respond(1); await first.pending;
  assert.equal(app.overlayRenders.length, 0);
  assert.equal(app.resources().sessionsStopped, 0);
  assert.equal(await app.runTimer(0), null, "일시중지 중 다음 프레임을 보내지 않는다");
  await app.action("pause");
  const next = await app.capture();
  assert.equal(app.frames[2].frameId, 2);
  await app.respond(2); await next.pending;
  assert.equal(app.overlayRenders.length, 1);
  await app.end();
});

test("추론 종료 원인과 영상 저장 확인 실패를 함께 표시하고 진단 기록에 종료 원인을 남긴다", async () => {
  const app = await harness();
  await app.action("start"); await app.action("record");
  const first = await app.capture();
  await app.respond(0); await first.pending;
  app.setNow(1100);
  const second = await app.capture();
  app.api.clips = async () => { throw new TypeError("Failed to fetch"); };
  app.frames[1].reject(Object.assign(new Error("JPEG decode rejected"), { status: 422 }));
  await second.pending;
  assert.equal(app.recordCounts().recording, false);
  assert.equal(app.resources().sessionsStopped, 1);
  assert.equal(app.retryVisible(), true);
  assert.match(app.statuses.at(-1), /JPEG decode rejected/);
  assert.match(app.statuses.at(-1), /영상.*실패.*Failed to fetch/);
  const failure = app.diagnostics.find(event => event.type === "frame_failure");
  const stopped = app.diagnostics.find(event => event.type === "session_stop");
  assert.ok(failure, "프레임 오류 진단을 남긴다");
  assert.ok(stopped, "세션 종료 진단을 남긴다");
  assert.equal(failure.error_message, "JPEG decode rejected");
  assert.equal(failure.http_status, 422);
  assert.equal(failure.frame_id, 2);
  assert.equal(stopped.reason, "frame_failed");
  assert.equal(stopped.error_message, "JPEG decode rejected");
});


test("기종·메모는 안내·일시중지·번호 입력·설정·종료 확인에서도 현재 세션에 저장한다", async () => {
  const app = await harness();
  await app.event("device", "change", "custom");
  await app.event("custom-device", "input", "  Galaxy S24  ");
  await app.event("note", "input", "  시작 메모  ");
  await app.action("start");
  assert.deepEqual(app.startSettings, [{ device_name: "Galaxy S24", note: "시작 메모" }]);
  for (const action of [null, "pause", "pause", "manual-arrival", "settings-type", "settings-home", "end"]) {
    if (action) await app.action(action);
    for (const id of ["device", "custom-device", "note"]) assert.equal(app.node(id).disabled, false);
    const note = `메모 ${app.metadataUpdates.length}`;
    await app.event("note", "input", `  ${note}  `);
    assert.equal(app.node("save-test-settings").disabled, false);
    assert.match(app.node("test-settings-status").textContent, /저장을 눌러/);
    await app.event("save-test-settings", "click");
    assert.deepEqual(app.metadataUpdates.at(-1), { session_id: "session-1", device_name: "Galaxy S24", note });
    assert.equal(app.node("save-test-settings").disabled, true);
    assert.match(app.node("test-settings-status").textContent, /현재 테스트에 저장된/);
  }
  assert.equal(app.resources().sessionsStarted, 1);
  await app.action("confirm-end");
});

test("빈 직접 입력은 거부하고 저장 실패 시 입력과 오류를 유지하여 다시 저장한다", async () => {
  const app = await harness();
  await app.action("start");
  await app.event("device", "change", "custom");
  await app.event("custom-device", "input", "   ");
  await app.event("save-test-settings", "click");
  assert.equal(app.metadataUpdates.length, 0);
  assert.match(app.node("test-settings-status").textContent, /기종을 입력/);
  await app.event("custom-device", "input", "  Galaxy S25  ");
  await app.event("note", "input", "  실패해도 유지할 메모  ");
  const save = app.api.updateMetadata;
  app.api.updateMetadata = async () => { throw new Error("연결 끊김"); };
  await app.event("save-test-settings", "click");
  assert.equal(app.node("note").value, "  실패해도 유지할 메모  ");
  assert.equal(app.node("save-test-settings").disabled, false);
  assert.match(app.node("test-settings-status").textContent, /저장하지 못했습니다.*연결 끊김/);
  await app.action("pause");
  assert.match(app.node("test-settings-status").textContent, /저장하지 못했습니다/);
  app.api.updateMetadata = save;
  await app.event("save-test-settings", "click");
  assert.deepEqual(app.metadataUpdates.at(-1), { session_id: "session-1", device_name: "Galaxy S25", note: "실패해도 유지할 메모" });
  await app.event("note", "input", "   ");
  await app.event("save-test-settings", "click");
  assert.equal(app.metadataUpdates.at(-1).note, "");
  await app.end();
});

test("시작·저장·종료 중 입력을 잠그고 중복 저장을 합치며 저장 응답 뒤 세션을 종료한다", async () => {
  const app = await harness();
  const start = app.api.start;
  let releaseStart;
  app.api.start = (...args) => new Promise(resolve => { releaseStart = () => resolve(start(...args)); });
  const starting = app.action("start");
  await flush();
  for (const id of ["device", "custom-device", "note"]) assert.equal(app.node(id).disabled, true);
  releaseStart(); await starting;
  await app.event("note", "input", "저장 중인 메모");
  let releaseSave, saveCalls = 0;
  app.api.updateMetadata = () => { saveCalls++; return new Promise(resolve => { releaseSave = resolve; }); };
  await app.event("save-test-settings", "click");
  await app.event("save-test-settings", "click");
  assert.equal(saveCalls, 1);
  for (const id of ["device", "custom-device", "note"]) assert.equal(app.node(id).disabled, true);
  assert.match(app.node("test-settings-status").textContent, /저장하고 있습니다/);
  let releaseStop;
  const stop = app.api.stop;
  app.api.stop = () => new Promise(resolve => { releaseStop = () => resolve(stop()); });
  const ending = app.end(); await flush();
  assert.equal(releaseStop, undefined, "저장 응답 전에는 서버 세션을 닫지 않는다");
  releaseSave({}); await flush();
  assert.equal(typeof releaseStop, "function");
  for (const id of ["device", "custom-device", "note"]) assert.equal(app.node(id).disabled, true);
  releaseStop(); await ending;
  for (const id of ["device", "custom-device", "note"]) assert.equal(app.node(id).disabled, false);
  assert.equal(app.resources().sessionsStopped, 1);
  assert.doesNotMatch(app.statuses.at(-1), /반영되지 않았습니다/);
});

test("저장하지 않았거나 종료를 기다리던 저장이 실패한 변경은 기록에 반영됐다고 표시하지 않는다", async () => {
  for (const pendingFailure of [false, true]) {
    const app = await harness();
    await app.action("start");
    await app.event("note", "input", "아직 저장되지 않은 메모");
    let rejectSave;
    if (pendingFailure) {
      app.api.updateMetadata = () => new Promise((_, reject) => { rejectSave = reject; });
      await app.event("save-test-settings", "click");
    }
    const ending = app.end(); await flush();
    if (pendingFailure) {
      assert.equal(app.resources().sessionsStopped, 0);
      rejectSave(new Error("저장 실패"));
    }
    await ending;
    assert.match(app.statuses.at(-1), /저장하지 않은 기종·메모.*반영되지 않았습니다/);
    assert.equal(app.node("note").value, "아직 저장되지 않은 메모");
    assert.equal(app.resources().sessionsStopped, 1);
  }
});
