const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness({ noAudio = false, playFailure = false, deferred = false, navigator = {},
  noSpeech = false, voices = [{ lang: "ko-KR" }], speechDeferred = false, speechThrow = false } = {}) {
  let audio = null, resolvePlayback = null, rejectPlayback = null, utterance = null, cancels = 0;
  const audioSources = [], playbackSources = [], utterances = [], connections = [], timers = new Map();
  let timerId = 0, clock = 0;
  const intervals = new Map();
  class Audio {
    constructor() { audio = this; this.loads = 0; this.pauses = 0; this.defaultPlaybackRate = this.playbackRate = 1; }
    pause() { this.pauses++; }
    load() { this.loads++; this.playbackRate = this.defaultPlaybackRate; }
    removeAttribute(name) { delete this[name]; }
    play() {
      playbackSources.push(this.src);
      if (deferred && this.src.startsWith("/api")) return new Promise((resolve, reject) => {
        resolvePlayback = resolve; rejectPlayback = reject;
      });
      if (playFailure) throw Object.assign(new Error("Missing MP3"), { name: "NotSupportedError" });
      this.onplaying?.();
    }
  }
  const settings = { audio: { volume: 1, playback_rate: 1.5, default_validity_ms: 8000,
    playback_timeout_ms: 15000, crosswalk_max_age_ms: 1500 } };
  const context = { window: { GConfig: { get: () => settings }, navigator, Audio: noAudio ? undefined : Audio,
    AudioContext: class {
      constructor() { this.destination = "speakers"; }
      createMediaStreamDestination() { return { stream: "recording-stream" }; }
      createMediaElementSource(element) {
        audioSources.push(element);
        return { connect(destination) { connections.push(destination); } };
      }
      resume() { return Promise.resolve(); }
    },
    SpeechSynthesisUtterance: noSpeech ? undefined : class { constructor(text) { this.text = text; } },
    speechSynthesis: noSpeech ? undefined : { getVoices: () => voices, speak(value) {
      utterance = value; utterances.push(value);
      if (speechThrow) throw new Error("Device TTS unavailable");
      if (!speechDeferred) value.onstart?.();
    },
      cancel() { cancels++; } },
  }, AbortController, performance: { now: () => clock },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, at: clock + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn) { const id = ++timerId; intervals.set(id, fn); return id; },
    clearInterval(id) { intervals.delete(id); } };
  vm.createContext(context);
  for (const name of ["tts", "audio_coordinator", "gps-motion", "gps-stop-select", "bus-journey"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  const player = context.window.GTts.create();
  const coordinator = context.window.GAudioCoordinator.create({ player });
  return { player, coordinator, audio: () => audio, utterance: () => utterance, cancels: () => cancels,
    journey: (options = {}) => context.window.GBusJourney.create({ coordinator, geolocation: {},
      now: () => 100000 + clock, monotonicNow: () => clock, ...options }),
    now: () => 100000 + clock,
    tick() { for (const fn of intervals.values()) fn(); coordinator.tick(); },
    audioFailure: () => rejectPlayback,
    audioSources, playbackSources, utterances, connections, timers,
    step(ms) {
      const end = clock + ms;
      for (;;) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        clock = next[1].at;
        timers.delete(next[0]);
        next[1].fn();
      }
      clock = end;
    },
    startAudio() { audio.onplaying?.(); resolvePlayback?.(); },
    finishAudio() { audio.ended = true; audio.onended?.(); },
    startSpeech() { utterance.onstart?.(); },
    failSpeech(error = "language-unavailable") { utterance.onerror?.({ error }); },
    rejectAudio() { rejectPlayback(Object.assign(new Error("late error"), { name: "NotSupportedError" })); } };
}

test("버스 동적 MP3도 기존 재생기와 녹화 오디오 그래프를 공유하고 재생 속도를 적용한다", () => {
  const app = harness();
  const message = "143번 버스가 3분 후 도착합니다.";
  assert.equal(app.player.recordingStream(), "recording-stream");
  assert.equal(app.player.setRate(1.25), 1.25);
  assert.equal(app.player.speak(message, 5000, { dynamic: true }), true);
  assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent(message)}`);
  assert.equal(app.audio().playbackRate, 1.25);
  assert.equal(app.player.recordingStream(), "recording-stream");
  assert.deepEqual(app.audioSources, [app.audio()]);
  assert.equal(app.connections.length, 2);
  app.player.cancel();
  app.player.setRate(10);
  assert.equal(app.player.getRate(), 2);
  app.player.setRate(.2);
  assert.equal(app.player.getRate(), .75);
  assert.equal(app.player.speak("멈추세요", 5000), true);
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v3");
  assert.equal(app.audio().playbackRate, .75);
  app.player.cancel();
});

test("확정된 다른 버스 이벤트가 전체 안내 경로를 거쳐 스피커·녹화용 MP3를 재생한다", () => {
  const app = harness();
  const journey = app.journey();
  app.player.recordingStream();
  app.coordinator.start();
  journey.start("7011");
  journey.accept({ status: "ready", captured_at_ms: 100000, event: { target_route: "7011",
    matches: [], recognized_routes: [{ track_id: 2, route_number: "604", state: "matched_candidate",
      token_score: .99, is_target: false }] } });
  const message = "604번 버스, 다른 노선.";
  const text = "다른 버스. 목표 버스를 계속 찾는 중.";
  assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent(text)}`);
  assert.equal(journey.snapshot().ocr.message, message);
  assert.equal(app.playbackSources.length, 1);
  assert.equal(app.connections.length, 2);
  assert.equal(app.utterance(), null);
  journey.stop();
  app.coordinator.stop();
});

for (const [state, message] of [["recognized_single", "7011번 버스 인식 중."],
  ["matched_candidate", "7011 목표 버스 확인."]]) {
  for (const [mode, options] of [["서버 MP3", {}], ["기기 음성 대체", { playFailure: true }],
    ["MP3 지원 없는 기기", { noAudio: true }], ["서버 지연 시 기기 음성", { deferred: true }]]) {
    test(`${mode}: ${state} 후보는 화면에 표시하고 확정된 목표 버스만 한 번 안내한다`, () => {
      const app = harness(options);
      const journey = app.journey();
      const event = { status: "ready", captured_at_ms: 100000, event: { target_route: "7011",
        matches: [{ track_id: 2, route_number: "7011", state, token_score: .99 }] } };
      const text = message;
      app.coordinator.start();
      journey.start("7011");
      journey.accept(event);
      if (state === "recognized_single") {
        app.step(1000); journey.accept(event);
        assert.equal(journey.snapshot().ocr.message, message);
        assert.deepEqual(app.playbackSources, []);
        assert.equal(app.utterance(), null);
        assert.equal(app.timers.size, 0);
        journey.stop(); app.coordinator.stop();
        return;
      }
      if (!options.noAudio) {
        assert.deepEqual(app.playbackSources, [`/api/bus-arrival-speech?text=${encodeURIComponent(text)}`]);
      }
      if (options.deferred) app.step(1000);
      assert.equal(journey.snapshot().ocr.message, message);
      const deviceSpeech = options.playFailure || options.noAudio || options.deferred;
      if (deviceSpeech) {
        assert.equal(app.utterance().text, text);
        assert.equal(app.utterance().lang, "ko-KR");
        assert.equal(app.utterances.length, 1);
      } else assert.equal(app.utterance(), null);
      journey.accept(event);
      if (deviceSpeech) app.utterance().onend();
      else { app.audio().ended = true; app.audio().onended(); }
      journey.accept(event);
      assert.equal(app.playbackSources.length, options.noAudio ? 0 : 1);
      assert.equal(app.utterances.length, deviceSpeech ? 1 : 0);
      assert.equal(app.timers.size, 0);
      journey.stop();
      app.coordinator.stop();
    });
  }
}

test("동적 MP3 재생 실패는 한국어 브라우저 음성으로 이어지고 취소 후 완료 콜백은 무시한다", () => {
  const app = harness({ playFailure: true });
  let completed = 0;
  app.player.setRate(1.2);
  assert.equal(app.player.speak("143번 버스 확인함. 143번 버스 확인함.", 5000,
    { dynamic: true, onEnd: () => completed++ }), true);
  assert.equal(app.utterance().lang, "ko-KR");
  assert.equal(app.utterance().rate, 1.2);
  app.player.cancel();
  assert.equal(app.cancels(), 1);
  app.utterance().onend();
  assert.equal(completed, 0);
  assert.equal(app.timers.size, 0);
});

test("HTML Audio 없는 환경에서도 버스 동적 안내를 브라우저 합성으로 재생한다", () => {
  const app = harness({ noAudio: true });
  assert.equal(app.player.speak("143번 버스 확인함. 143번 버스 확인함.", 5000, { dynamic: true }), true);
  assert.match(app.utterance().text, /143번/);
  app.player.cancel();
  assert.equal(app.player.recordingStream(), null);
});

test("긴급 음성이 동적 MP3를 취소한 뒤 늦은 MP3 오류가 버스 합성을 되살리지 않는다", async () => {
  const app = harness({ deferred: true });
  app.coordinator.start();
  app.coordinator.request({ source: "bus-ocr", priority: app.coordinator.PRIORITY.busOcr,
    dynamic: true, text: "143번 버스 인식 중. 143번 버스 인식 중.", validUntil: 3000 });
  assert.ok(app.audio().src.startsWith("/api/bus-arrival-speech"));
  app.coordinator.request({ source: "walking", priority: app.coordinator.PRIORITY.emergency,
    text: "멈추세요.", validUntil: 3000 });
  app.rejectAudio();
  await Promise.resolve();
  assert.equal(app.utterance(), null);
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v3");
  app.coordinator.stop();
});

test("버스 MP3 로딩이 멈추면 촬영 결과가 만료되기 전에 기기 한국어 음성으로 안내한다", async () => {
  const app = harness({ deferred: true });
  let started = 0, completed = 0, failed = 0;
  const text = "7011번 버스 확인함. 7011번 버스 확인함.";
  app.player.speak(text, 3000, { dynamic: true, onStart: () => started++,
    onEnd: () => completed++, onFailure: () => failed++ });
  app.step(999);
  assert.equal(app.utterance(), null);
  app.step(1);
  assert.equal(app.utterance().text, text);
  assert.equal(app.utterance().lang, "ko-KR");
  assert.equal(started, 1);
  app.rejectAudio();
  await Promise.resolve();
  assert.equal(failed, 0, "취소된 서버 음원의 늦은 오류가 기기 안내를 끊지 않는다");
  app.step(2500);
  assert.equal(app.cancels(), 0, "유효한 프레임에서 시작한 문장은 끝까지 읽는다");
  app.utterance().onend();
  assert.equal(completed, 1);
  assert.equal(app.timers.size, 0);
});

test("남은 OCR 유효 시간이 짧아도 절반을 기기 음성 시작에 남긴다", () => {
  const app = harness({ deferred: true });
  app.player.speak("7011번 버스 확인함. 7011번 버스 확인함.", 200, { dynamic: true });
  app.step(99);
  assert.equal(app.utterance(), null);
  app.step(1);
  assert.ok(app.utterance());
  app.player.cancel();
  assert.equal(app.timers.size, 0);
});

test("서버 MP3가 시작했거나 안내가 취소되면 지연 대체 음성을 재생하지 않는다", () => {
  const playing = harness();
  playing.player.speak("7011번 버스 확인함. 7011번 버스 확인함.", 3000, { dynamic: true });
  playing.step(1200);
  assert.equal(playing.utterance(), null);
  playing.player.cancel();
  const cancelled = harness({ deferred: true });
  cancelled.player.speak("7011번 버스 확인함. 7011번 버스 확인함.", 3000, { dynamic: true });
  cancelled.player.cancel();
  cancelled.step(1200);
  assert.equal(cancelled.utterance(), null);
  assert.equal(cancelled.timers.size, 0);
});

const android = { userAgent: "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/140.0" };

const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
const busSpeechSource = text => `/api/bus-arrival-speech?text=${encodeURIComponent(text)}`;

async function gpsJourney(app) {
  let receivePosition;
  const events = [];
  const geolocation = { watchPosition(success) { receivePosition = success; return 1; }, clearWatch() {} };
  const api = { nearbyBusArrival: async () => ({ matches: [{
    station: { station_id: "station-a", station_name: "테스트 정류장", latitude: 37.5,
      longitude: 127, distance_m: 0 }, bus_route_id: "route-773",
    arrival: { route_id: "route-773", direction: "종점", station_order: 1,
      first_arrival: "3분 후", first_arrival_state: "approaching", first_vehicle_id: "vehicle-1" },
    announcement: "테스트 정류장에 773번 버스가 3분 후 도착합니다.",
  }] }) };
  const journey = app.journey({ api, geolocation, onEvent: event => events.push(event) });
  app.coordinator.start();
  journey.start("773");
  receivePosition({ timestamp: app.now(), coords: { latitude: 37.5, longitude: 127, accuracy: 5 } });
  await flush();
  assert.equal(journey.snapshot().gps.status, "ready");
  const detect = ({ target = false, state = "matched_candidate", id = 4 } = {}) => {
    const observation = { track_id: id, route_number: target ? "773" : "7011", state,
      token_score: .99, is_target: target };
    journey.accept({ status: "ready", captured_at_ms: app.now(), event: { target_route: "773",
      matches: target ? [observation] : [], recognized_routes: target ? [] : [observation] } });
  };
  return { journey, detect, events };
}

test("773 도착정보 MP3 재생 중 목표 773 확인은 이전 음성을 끊고 확인 안내로 교체한다", async () => {
  const app = harness();
  const { journey, detect, events } = await gpsJourney(app);
  assert.equal(app.audio().src, busSpeechSource("도착정보. 773번, 3분 후."));
  const loads = app.audio().loads, pauses = app.audio().pauses;
  const latePlaying = app.audio().onplaying, lateEnded = app.audio().onended;
  detect({ target: true });
  const targetSource = busSpeechSource("773 목표 버스 확인.");
  assert.equal(app.audio().src, targetSource);
  assert.equal(app.audio().pauses, pauses + 1);
  assert.ok(app.audio().loads > loads, "목표 버스 확인 음원을 새로 로딩한다");
  assert.deepEqual(app.playbackSources, [busSpeechSource("도착정보. 773번, 3분 후."), targetSource]);
  latePlaying?.(); lateEnded?.();
  assert.equal(app.audio().src, targetSource);
  assert.equal(events.filter(event => event.type === "bus_arrival_speech_completed").length, 0);
  assert.equal(events.filter(event => event.type === "bus_ocr_speech_started").length, 1);
  app.finishAudio(); app.tick();
  assert.equal(app.playbackSources.length, 2, "확인 뒤 이전 도착정보를 자동으로 재생하지 않는다");
  journey.stop(); app.coordinator.stop();
});

test("773 도착정보 MP3 재생 중 목표 번호 후보는 현재 음원을 중단하지 않는다", async () => {
  const app = harness();
  const { journey, detect } = await gpsJourney(app);
  const source = app.audio().src, loads = app.audio().loads, pauses = app.audio().pauses;
  detect({ target: true, state: "recognized_single" }); app.tick();
  assert.equal(journey.snapshot().ocr.confirmed, false);
  assert.equal(app.audio().src, source);
  assert.equal(app.audio().loads, loads);
  assert.equal(app.audio().pauses, pauses);
  assert.deepEqual(app.playbackSources, [source]);
  assert.equal(app.utterance(), null);
  journey.stop(); app.coordinator.stop();
});

test("773 도착정보 MP3 재생 중 7011 인식은 음원을 유지하고 종료 뒤에도 다른 버스 안내를 지연 재생하지 않는다", async () => {
  const app = harness();
  const { journey, detect } = await gpsJourney(app);
  const source = app.audio().src, loads = app.audio().loads, pauses = app.audio().pauses;
  detect(); app.step(500); app.tick();
  assert.equal(journey.snapshot().ocr.routeNumber, "7011");
  assert.equal(journey.snapshot().ocr.isTarget, false);
  assert.equal(app.audio().src, source);
  assert.equal(app.audio().loads, loads);
  assert.equal(app.audio().pauses, pauses);
  assert.deepEqual(app.playbackSources, [source]);
  app.finishAudio(); app.tick();
  app.step(500); detect({ id: 5 }); app.tick();
  assert.deepEqual(app.playbackSources, [source], "최신 773 도착정보가 있으면 7011 안내를 뒤늦게 재생하지 않는다");
  assert.equal(app.utterance(), null);
  journey.stop(); app.coordinator.stop();
});

test("7011 인식 후 다시 듣기도 최신 773 도착정보 MP3를 재생한다", async () => {
  const app = harness();
  const { journey, detect } = await gpsJourney(app);
  const source = app.audio().src;
  detect(); app.finishAudio();
  assert.equal(journey.repeat(), true);
  assert.equal(app.audio().src, source);
  assert.deepEqual(app.playbackSources, [source, source]);
  app.finishAudio(); app.tick();
  assert.equal(app.playbackSources.length, 2);
  journey.stop(); app.coordinator.stop();
});

test("773 확인으로 전환한 뒤 이전 도착정보 MP3·Android 기기 합성의 늦은 콜백은 현재 확인 안내를 끊지 않는다", async () => {
  const app = harness({ navigator: android, deferred: true, speechDeferred: true });
  const { journey, detect, events } = await gpsJourney(app);
  const latePlaying = app.audio().onplaying, lateEnded = app.audio().onended;
  const lateReject = app.audioFailure();
  app.step(1000);
  const oldSpeech = app.utterance();
  assert.equal(oldSpeech.text, "도착정보. 773번, 3분 후.");
  const lateStart = oldSpeech.onstart, lateEnd = oldSpeech.onend, lateError = oldSpeech.onerror;
  detect({ target: true });
  const targetSource = busSpeechSource("773 목표 버스 확인.");
  assert.equal(app.audio().src, targetSource);
  app.step(1000);
  const targetSpeech = app.utterance();
  assert.equal(targetSpeech.text, "773 목표 버스 확인.");
  assert.notEqual(targetSpeech, oldSpeech);
  const loads = app.audio().loads, pauses = app.audio().pauses, cancels = app.cancels();
  latePlaying?.(); lateEnded?.(); lateStart?.(); lateEnd?.(); lateError?.({ error: "canceled" });
  lateReject(Object.assign(new Error("late GPS error"), { name: "NotSupportedError" }));
  await flush();
  assert.equal(app.audio().src, targetSource);
  assert.equal(app.audio().loads, loads);
  assert.equal(app.audio().pauses, pauses);
  assert.equal(app.cancels(), cancels);
  assert.equal(events.filter(event => event.type === "bus_arrival_speech_completed").length, 0);
  app.startSpeech();
  assert.equal(events.filter(event => event.type === "bus_ocr_speech_started").length, 1);
  targetSpeech.onend?.();
  assert.equal(events.filter(event => event.type === "bus_ocr_speech_completed").length, 1);
  assert.equal(app.timers.size, 0);
  journey.stop(); app.coordinator.stop();
});

for (const [mode, options] of [["한국어 엔진 초기화 대기", { voices: [], speechDeferred: true }],
  ["기기 합성 시작 대기", { speechDeferred: true }], ["기기 합성 호출 실패", { speechThrow: true }],
  ["기기 합성 API 없음", { noSpeech: true }]]) {
  test(`Android ${mode}: 늦게 시작하는 서버 MP3를 끊지 않는다`, async () => {
    const app = harness({ navigator: android, deferred: true, ...options });
    let started = 0, completed = 0, failed = 0;
    app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onStart: () => started++,
      onEnd: () => completed++, onFailure: () => failed++ });
    const source = app.audio().src;
    app.step(1000);
    assert.equal(app.audio().src, source, "합성 요청만으로 로딩 중인 MP3를 취소하지 않는다");
    assert.equal(app.audio().pauses, 0);
    assert.equal(started, 0);
    assert.equal(failed, 0);
    app.step(800); app.startAudio(); await Promise.resolve();
    assert.equal(started, 1);
    app.finishAudio();
    assert.equal(completed, 1);
    assert.equal(failed, 0);
    assert.equal(app.timers.size, 0);
  });
}

test("Android 기기 합성이 언어 오류를 보고해도 로딩 중인 서버 MP3로 안내한다", async () => {
  const app = harness({ navigator: android, deferred: true, speechDeferred: true, voices: [] });
  let started = 0, completed = 0, failed = 0;
  app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onStart: () => started++,
    onEnd: () => completed++, onFailure: () => failed++ });
  const source = app.audio().src;
  app.step(1000); app.failSpeech();
  assert.equal(app.audio().src, source);
  assert.equal(failed, 0, "기기 합성 오류가 유효한 서버 음원까지 실패시키지 않는다");
  app.step(600); app.startAudio(); await Promise.resolve(); app.finishAudio();
  assert.equal(started, 1);
  assert.equal(completed, 1);
  assert.equal(failed, 0);
  assert.equal(app.timers.size, 0);
});

test("서버 MP3가 먼저 시작하면 대기 중인 기기 합성을 취소하고 늦은 이벤트를 무시한다", async () => {
  const app = harness({ navigator: android, deferred: true, speechDeferred: true });
  let started = 0, completed = 0, failed = 0;
  app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onStart: () => started++,
    onEnd: () => completed++, onFailure: () => failed++ });
  const source = app.audio().src;
  app.step(1000);
  const lateStart = app.utterance().onstart, lateEnd = app.utterance().onend, lateError = app.utterance().onerror;
  app.startAudio(); await Promise.resolve();
  assert.equal(app.cancels(), 1);
  lateStart?.(); lateEnd?.(); lateError?.({ error: "canceled" });
  assert.equal(app.audio().src, source);
  assert.equal(started, 1);
  assert.equal(completed, 0);
  assert.equal(failed, 0);
  app.finishAudio();
  assert.equal(completed, 1);
  assert.equal(app.timers.size, 0);
});

test("기기 합성이 실제 시작한 뒤에만 서버 MP3를 취소하고 늦은 서버 이벤트를 무시한다", async () => {
  const app = harness({ navigator: android, deferred: true, speechDeferred: true });
  let started = 0, completed = 0, failed = 0;
  app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onStart: () => started++,
    onEnd: () => completed++, onFailure: () => failed++ });
  const source = app.audio().src, latePlaying = app.audio().onplaying, lateEnded = app.audio().onended;
  app.step(1000);
  assert.equal(app.audio().src, source);
  assert.equal(started, 0);
  app.step(200); app.startSpeech();
  assert.equal(app.audio().src, undefined);
  assert.equal(app.audio().pauses, 1);
  assert.equal(started, 1);
  latePlaying?.(); app.audio().ended = true; lateEnded?.(); app.rejectAudio(); await Promise.resolve();
  assert.equal(started, 1);
  assert.equal(completed, 0, "취소된 서버 음원의 종료 이벤트가 기기 합성을 완료시키지 않는다");
  assert.equal(failed, 0);
  app.utterance().onend?.();
  assert.equal(completed, 1);
  assert.equal(app.timers.size, 0);
});

test("서버와 기기 음성이 모두 시작하지 않으면 유효 시간에 한 번 실패하고 늦은 시작을 무시한다", async () => {
  const app = harness({ navigator: android, deferred: true, speechDeferred: true });
  let started = 0, completed = 0, failed = 0;
  app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onStart: () => started++,
    onEnd: () => completed++, onFailure: () => failed++ });
  const latePlaying = app.audio().onplaying;
  app.step(1000);
  const lateStart = app.utterance().onstart, lateEnd = app.utterance().onend;
  app.step(2000);
  assert.equal(failed, 1);
  assert.equal(app.timers.size, 0);
  assert.equal(app.cancels(), 1);
  latePlaying?.(); lateStart?.(); lateEnd?.(); app.rejectAudio(); await Promise.resolve();
  assert.equal(started, 0);
  assert.equal(completed, 0);
  assert.equal(failed, 1);
});

test("MP3 지원 없이 기기 합성도 실패하면 안내 실패를 한 번 보고한다", () => {
  const app = harness({ navigator: android, noAudio: true, speechDeferred: true });
  let failed = 0;
  app.player.speak("7011 목표 버스 확인.", 3000, { dynamic: true, onFailure: () => failed++ });
  app.failSpeech(); app.failSpeech();
  assert.equal(failed, 1);
  assert.equal(app.timers.size, 0);
});

for (const [mode, options] of [["기기 합성 API 없음", { noSpeech: true }],
  ["한국어 엔진 없음", { voices: [], speechDeferred: true }]]) {
  test(`Android ${mode}: 버스 번호 질문도 서버 MP3로 안내한다`, () => {
    const app = harness({ navigator: android, ...options });
    const question = "정류장입니다. 버스를 선택하세요.";
    let started = 0, completed = 0;
    assert.equal(app.player.speak(question, 8000, { onStart: () => started++, onEnd: () => completed++ }), true);
    assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent(question)}`);
    assert.equal(app.utterance(), null);
    assert.equal(started, 1);
    app.finishAudio();
    assert.equal(completed, 1);
    assert.equal(app.timers.size, 0);
  });
}

const iphone = { userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1" };
for (const [name, navigator] of [["iPhone Safari", iphone],
  ["iPhone Chrome", { userAgent: "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 CriOS/140.0 Mobile Safari/604.1" }],
  ["iPad", { userAgent: "Mozilla/5.0 (iPad) AppleWebKit/605.1.15" }],
  ["데스크톱 UA iPad", { userAgent: "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15", platform: "MacIntel", maxTouchPoints: 5 }]]) {
  test(`${name}: 녹화 그래프에서도 배속 음원을 1배로 재생한다`, () => {
    const app = harness({ navigator });
    for (const rate of [.75, 1, 1.5, 2]) {
      app.player.setRate(rate);
      assert.equal(app.player.getRate(), rate);
      assert.equal(app.player.recordingStream(), "recording-stream");
      let ends = 0;
      app.player.speak("오른쪽 두 걸음", 5000, { onEnd: () => ends++ });
      assert.equal(app.audio().src, rate === 1 ? "/audio/walking-move-right-two.mp3?v=sesac-212-v3"
        : `/api/guidance-speech?clip=walking-move-right-two&rate=${rate}`);
      assert.equal(app.audio().defaultPlaybackRate, 1);
      assert.equal(app.audio().playbackRate, 1);
      app.audio().ended = true; app.audio().onended();
      assert.equal(ends, 1);
      assert.equal(app.timers.size, 0);
      app.player.speak("143번 버스 확인함.", 5000, { dynamic: true });
      assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent("143번 버스 확인함.")}&rate=${rate}`);
      assert.equal(app.audio().playbackRate, 1);
      app.player.cancel();
    }
    assert.deepEqual(app.audioSources, [app.audio()], "스피커와 녹화가 동일 재생기를 계속 공유한다");
    assert.equal(app.connections.length, 2);
  });
}

for (const navigator of [{ userAgent: "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/140.0" },
  { userAgent: "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15", platform: "MacIntel", maxTouchPoints: 0 }]) {
  test(`${navigator.userAgent}: 기존 브라우저 배속과 음원 URL을 유지한다`, () => {
    const app = harness({ navigator });
    app.player.unlock();
    assert.equal(app.audio().loads, 0);
    assert.equal(app.connections.length, 2, "시작 버튼에서 모든 플랫폼의 스피커·녹화 그래프를 준비한다");
    app.player.recordingStream();
    for (const rate of [1, 1.5, 2]) {
      app.player.setRate(rate);
      app.player.speak("멈추세요", 5000);
      assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v3");
      assert.equal(app.audio().defaultPlaybackRate, rate);
      assert.equal(app.audio().playbackRate, rate);
      app.player.cancel();
      app.player.speak("143번 버스 확인함.", 5000, { dynamic: true });
      assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent("143번 버스 확인함.")}`);
      assert.equal(app.audio().playbackRate, rate);
      app.player.cancel();
    }
  });
}

test("iPhone 음성 준비와 배속 변경은 진행 중인 안내를 다시 로딩하지 않는다", () => {
  const app = harness({ navigator: iphone });
  app.player.unlock();
  assert.equal(app.audio().loads, 1);
  assert.equal(app.connections.length, 2);
  app.player.setRate(1.5);
  app.player.speak("멈추세요", 5000);
  const source = app.audio().src, loads = app.audio().loads;
  app.player.unlock(); app.player.recordingStream(); app.player.setRate(2);
  assert.equal(app.audio().src, source);
  assert.equal(app.audio().loads, loads);
  assert.equal(app.audio().playbackRate, 1);
  app.player.cancel();
  app.player.speak("멈추세요", 5000);
  assert.equal(app.audio().src, "/api/guidance-speech?clip=walking-stop&rate=2");
  app.player.cancel();
});

test("iPhone 서버 음성 실패 시 기기 합성은 선택 배속을 유지한다", () => {
  const app = harness({ navigator: iphone, playFailure: true });
  app.player.setRate(2);
  app.player.speak("143번 버스 확인함.", 5000, { dynamic: true });
  assert.equal(app.utterance().rate, 2);
  app.player.cancel();
});
