const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function harness() {
  let now = 100, nextTimer = 0, screen = "home", cameraActive = false, cameraEnded;
  let handlers, voice = null, busState = { status: "idle", revision: 0, arrival_event_id: null };
  let journeyCallbacks, journeyPaused = false, route = null;
  const timers = new Map(), intervals = new Map(), frames = [], guides = [], journeyStarts = [], renders = [], statuses = [];
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, { value: id === "device" ? "test phone" : "", style: {},
      classList: { toggle() {} }, addEventListener() {} });
    return elements.get(id);
  };
  const view = { setSettings() {}, show(value) { screen = value; }, getScreen: () => screen,
    setBusy() {}, setStatus(text) { statuses.push(text); }, announce() {}, setRoute() {}, setBus() {},
    setStations() {}, setPaused() {}, render(value) { renders.push(value); }, getGuidance: () => "안내" };
  const player = { setRate: value => value, recordingStream: () => null, cancel() { voice = null; },
    speak(text, validUntil, callbacks) { voice = { text, validUntil, ...callbacks }; callbacks.onStart?.(); return true; } };
  const settings = { audio: { tick_ms: 250, playback_timeout_ms: 15000, crosswalk_max_age_ms: 1500 },
    camera: { capture_max_side: 640, jpeg_quality: .72 } };
  const journey = { start(value) {
    route = value; journeyStarts.push(value); journeyPaused = false;
    journeyCallbacks.onChange({ route, gps: { status: "locating", candidates: [] }, ocr: {} });
  }, pause() { journeyPaused = true; }, resume() { if (route) journeyPaused = false; },
    stop() { route = null; journeyPaused = false; }, accept() {}, selectStop() {}, repeat() {} };
  const api = { start: async () => ({ session_id: "session-1" }), stop: async () => ({ frame_count: frames.length }),
    frame(id, frameId, capturedAtMs, blob, signal) {
      return new Promise((resolve, reject) => frames.push({ id, frameId, capturedAtMs, blob, signal, resolve, reject }));
    },
    async boarding(_id, action, _event, number) {
      if (action === "arrive") busState = { status: "awaiting_stop", arrival_event_id: 1,
        revision: busState.revision + 1, arrival_source: "user_confirmed", bus_number: null };
      else {
        const state = { stop_announced: "pending", submit: "submitted", reopen: "awaiting_stop", cancel: "cancelled" }[action];
        busState = { ...busState, revision: busState.revision + 1, status: state,
          bus_number: action === "submit" ? number : null };
      }
      return { ...busState };
    },
    timings: async () => ({}), busEvents: async () => ({}), recordingEvent: async () => ({}),
    recording: async () => ({}), camera: async () => ({}),
  };
  const context = {
    console, AbortController, Date: { now: () => 100000 + now },
    performance: { now: () => now, timeOrigin: 100000 },
    localStorage: { getItem: () => null, setItem() {} },
    document: { hidden: false, getElementById: node, addEventListener() {} },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { const id = ++nextTimer; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    GView: { create(options) { handlers = options; return view; } },
    GTts: { create: () => player }, GApi: api, GConfig: { get: () => settings, load: async () => settings },
    GGuidance: { create() { const guide = { accepted: [], start() {}, stop() {}, tick() {},
      accept(value) { this.accepted.push(value); } }; guides.push(guide); return guide; } },
    GBusJourney: { create(options) { journeyCallbacks = options; return journey; } },
    GCamera: { async start() { cameraActive = true; return { width: 640, height: 480 }; },
      stop() { cameraActive = false; }, active: () => cameraActive, capture: async () => ({ size: 12 }),
      pause() {}, resume() {}, setOnEnded(callback) { cameraEnded = callback; } },
    GRecorder: { prepareAudio() {}, cancelPreparedAudio() {}, startPreview() {}, stopPreview() {},
      startRaw() {}, start() {}, pause() {}, resume() {}, stop: async () => null, stopRaw: async () => null },
    GOverlay: { size() {}, clear() {}, render(_result, done) { done("drawn"); } },
    fetch: async () => ({}), addEventListener() {},
  };
  context.window = context;
  vm.createContext(context);
  for (const name of ["audio_coordinator", "boarding"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  await vm.runInContext(fs.readFileSync("frontend/js/app.js", "utf8"), context);
  const action = async value => { handlers.onAction(value); await flush(); };
  const frameResult = frame => ({ session_id: "session-1", frame_id: frame.frameId,
    captured_at_ms: frame.capturedAtMs, inference_ms: 10,
    walking: { detections: [], event: {} }, traffic: { detections: [], event: {} },
    boarding: { ...busState }, stop_proximity: { nearby: false } });
  return { action, frames, guides, journeyStarts, renders, statuses, screen: () => screen,
    journeyPaused: () => journeyPaused, setNow(value) { now = value; },
    async capture() {
      const [id, callback] = [...timers][0]; timers.delete(id);
      const pending = callback(); await flush(); return { pending };
    },
    async respond(index) { frames[index].resolve(frameResult(frames[index])); await flush(); },
    async finishVoice() { const done = voice; voice = null; done.onEnd(); await flush(); },
    async confirmRoute(value) { handlers.onSubmitRoute(value); await flush(); await action("confirm-route"); },
    async cameraEnd() { cameraActive = false; cameraEnded(); await flush(); },
  };
}

test("정류장 수동 확인과 번호 확정은 서버 boarding을 거쳐 GPS/OCR 여정을 한 번 시작한다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  assert.equal(app.screen(), "input");
  await app.confirmRoute("143");
  assert.equal(app.screen(), "search");
  assert.deepEqual(app.journeyStarts, ["143"]);
  await app.action("end");
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
  await app.action("end");
});

test("카메라 종료 후 GPS만 계속할 때도 일시중지 버튼으로 GPS 안내를 멈출 수 있다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  await app.cameraEnd();
  assert.equal(app.journeyPaused(), false, "카메라 종료와 GPS 안내는 독립적이다");
  await app.action("pause");
  assert.equal(app.journeyPaused(), true, "사용자 일시중지는 GPS도 멈춘다");
  await app.action("end");
});
