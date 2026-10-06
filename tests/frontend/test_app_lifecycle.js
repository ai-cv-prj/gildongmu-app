const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function harness({ stopFailsOnce = false, clipState = "ready", uploadResponseLostOnce = false } = {}) {
  let now = 100, nextTimer = 0, screen = "home", cameraActive = false, cameraEnded;
  let handlers, voice = null, busState = { status: "idle", revision: 0, arrival_event_id: null }, routeInput = "";
  let journeyCallbacks, journeyPaused = false, route = null;
  let sessionsStarted = 0, sessionsStopped = 0, stopAttempts = 0, uploadAttempts = 0;
  let recording = false, recordingStartedAt = null, recorderEnded = null;
  let recordStarts = 0, recordStops = 0;
  const timeline = [], uploads = [];
  const timers = new Map(), intervals = new Map(), frames = [], guides = [], journeyStarts = [], renders = [], statuses = [];
  const captures = [], startModes = [];
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, { value: id === "device" ? "test phone" : "", style: {},
      classList: { toggle() {} }, addEventListener() {} });
    return elements.get(id);
  };
  const view = { setSettings() {}, show(value) { screen = value; }, getScreen: () => screen,
    setObstacleDetection() {}, setBusy() {}, setStatus(text) { statuses.push(text); }, announce() {}, setRoute(value) { routeInput = value; }, setBus() {},
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
    stop() { route = null; journeyPaused = false; }, accept() {}, selectStop() {}, repeat() {} };
  const api = { start: async (_device, _note, busHighres) => {
    sessionsStarted++; startModes.push(busHighres); return { session_id: "session-1" }; },
    stop: async () => {
      stopAttempts++;
      if (stopFailsOnce && stopAttempts === 1) throw Object.assign(new Error("일시적 연결 실패"), { status: 503 });
      sessionsStopped++; timeline.push("stop"); return { frame_count: frames.length };
    },
    frame(id, frameId, capturedAtMs, blob, signal, busBlob, busCapturedAtMs) {
      return new Promise((resolve, reject) => frames.push({ id, frameId, capturedAtMs, blob, signal,
        busBlob, busCapturedAtMs, resolve, reject }));
    },
    async boarding(_id, action, _event, number) {
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
    localStorage: { getItem: () => null, setItem() {} },
    document: { hidden: false, getElementById: node, addEventListener() {} },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { const id = ++nextTimer; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    GView: { create(options) { handlers = options; return view; } },
    GTts: { create: () => player }, GApi: api, GConfig: { get: () => settings, load: async () => settings },
    GGuidance: { create() { const guide = { accepted: [], starts: 0, stops: 0, start() { this.starts++; }, stop() { this.stops++; }, tick() {},
      accept(value) { this.accepted.push(value); } }; guides.push(guide); return guide; } },
    GBusJourney: { create(options) { journeyCallbacks = options; return journey; } },
    GCamera: { video: { videoWidth: 1280, videoHeight: 720 },
      async start() { cameraActive = true; return { width: 1280, height: 720 }; },
      stop() { cameraActive = false; }, active: () => cameraActive,
      capture: async maxSide => { captures.push(maxSide); return { size: 12, maxSide }; },
      pause() {}, resume() {}, setOnEnded(callback) { cameraEnded = callback; } },
    GRecorder: { startPreview() {}, stopPreview() {}, pause() {}, resume() {}, bytes: () => 0,
      startRaw(_onChunk, onEnded) { recording = true; recordStarts++; recorderEnded = onEnded;
        recordingStartedAt = 100000 + now; return recordingStartedAt; },
      async stopRaw() { if (!recording) return null; recording = false; recordStops++;
        return { blob: { size: 12, type: "video/webm" }, started_at_ms: recordingStartedAt,
          ended_at_ms: Math.min(100000 + now, recordingStartedAt + 30000) }; } },
    GOverlay: { size() {}, clear() {}, snapshot: () => "cG5n",
      render(_result, done) { done("drawn"); } },
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
  return { action, frames, captures, startModes, guides, journeyStarts, renders, statuses, uploads, timeline,
    recordCounts: () => ({ starts: recordStarts, stops: recordStops, recording }), screen: () => screen,
    retryVisible: () => !node("retry-upload").hidden,
    resources: () => ({ cameraActive, sessionsStarted, sessionsStopped, timers: timers.size, intervals: intervals.size, route }),
    journeyPaused: () => journeyPaused, routeInput: () => routeInput, setNow(value) { now = value; },
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
    async draftRoute(value) { handlers.onSubmitRoute(value); await flush(); },
    async confirmRoute(value) { handlers.onSubmitRoute(value); await flush(); await action("confirm-route"); },
    async cameraEnd() { cameraActive = false; cameraEnded(); await flush(); },
    async recorderError() { recording = false; recordStops++; recorderEnded?.(new Error("인코더 오류")); await flush(); },
  };
}

test("정류장 수동 확인과 번호 확정은 서버 boarding을 거쳐 GPS/OCR 여정을 한 번 시작한다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  assert.equal(app.screen(), "input", "멈춤 안내와 동시에 번호 입력 화면을 연다");
  await app.confirmRoute("143");
  assert.equal(app.screen(), "confirm");
  assert.deepEqual(app.journeyStarts, [], "멈춤 안내 중에는 버스 찾기를 시작하지 않는다");
  await app.finishVoice();
  assert.equal(app.screen(), "confirm", "멈춤 안내가 끝나도 작성 중인 확인 화면을 유지한다");
  await app.action("confirm-route");
  assert.equal(app.screen(), "search");
  assert.deepEqual(app.journeyStarts, ["143"]);
  await app.action("end");
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
  await app.action("end");
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
  await app.action("end");
});

test("안내 종료는 카메라와 세션을 정리한 뒤 마무리를 표시하고 처음으로 돌아간다", async () => {
  const app = await harness();
  await app.action("enter");
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.confirmRoute("143");
  await app.action("end");
  assert.equal(app.screen(), "finish");
  assert.deepEqual(app.resources(), { cameraActive: false, sessionsStarted: 1, sessionsStopped: 1,
    timers: 0, intervals: 0, route: null });
  await app.action("finish-home");
  assert.equal(app.screen(), "welcome");
  assert.equal(app.resources().cameraActive, false);
  await app.action("enter");
  await app.action("start");
  assert.equal(app.screen(), "walk");
  assert.equal(app.resources().sessionsStarted, 2);
  await app.action("end");
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
  await app.action("back");
  assert.equal(app.resources().route, null, "이전이 무시되지 않고 노선 안내를 해제한다");
  assert.equal(app.journeyPaused(), false);
  assert.equal(app.resources().cameraActive, true, "노선을 다시 고르는 동안 세션을 유지한다");
  assert.equal(app.resources().sessionsStopped, 0);
  await app.action("end");
});

test("새 정류장 도착에서는 이전 도착 때 입력한 버스 번호를 지운다", async () => {
  const app = await harness();
  await app.action("start");
  await app.action("manual-arrival");
  await app.finishVoice();
  await app.draftRoute("143");
  assert.equal(app.screen(), "confirm");
  await app.action("back");
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "143", "번호 확인에서 돌아오면 입력한 번호를 유지한다");
  await app.action("back");
  assert.equal(app.screen(), "walk");
  await app.action("manual-arrival");
  await app.finishVoice();
  assert.equal(app.screen(), "input");
  assert.equal(app.routeInput(), "", "새 도착에서는 입력란을 비운다");
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

  await app.action("end");
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
  await app.action("end");
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
  await app.action("end");
});

test("영상이 없어도 종료 요청 실패를 화면에서 다시 시도할 수 있다", async () => {
  const app = await harness({ stopFailsOnce: true });
  await app.action("start");
  await app.action("end");
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
  await app.action("end");
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
  await app.action("end");
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
  await app.action("end");
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
  await app.action("end");
});
