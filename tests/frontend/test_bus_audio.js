const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness({ noAudio = false, playFailure = false, deferred = false, navigator = {} } = {}) {
  let audio = null, rejectPlayback = null, utterance = null, cancels = 0;
  const audioSources = [], playbackSources = [], utterances = [], connections = [], timers = new Map();
  let timerId = 0, clock = 0;
  class Audio {
    constructor() { audio = this; this.loads = 0; this.defaultPlaybackRate = this.playbackRate = 1; }
    pause() {}
    load() { this.loads++; this.playbackRate = this.defaultPlaybackRate; }
    removeAttribute(name) { delete this[name]; }
    play() {
      playbackSources.push(this.src);
      if (deferred && this.src.startsWith("/api")) return new Promise((_, reject) => { rejectPlayback = reject; });
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
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    speechSynthesis: { getVoices: () => [{ lang: "ko-KR" }], speak(value) {
      utterance = value; utterances.push(value); value.onstart();
    },
      cancel() { cancels++; } },
  }, performance: { now: () => clock },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, at: clock + delay }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval: () => 1, clearInterval() {} };
  vm.createContext(context);
  for (const name of ["tts", "audio_coordinator", "gps-motion", "gps-stop-select", "bus-journey"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  const player = context.window.GTts.create();
  const coordinator = context.window.GAudioCoordinator.create({ player });
  return { player, coordinator, audio: () => audio, utterance: () => utterance, cancels: () => cancels,
    journey: () => context.window.GBusJourney.create({ coordinator, geolocation: {},
      now: () => 100000 + clock, monotonicNow: () => clock }),
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
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v2");
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
  const text = `${message} ${message}`;
  assert.equal(app.audio().src, `/api/bus-arrival-speech?text=${encodeURIComponent(text)}`);
  assert.equal(journey.snapshot().ocr.message, message);
  assert.equal(app.playbackSources.length, 1);
  assert.equal(app.connections.length, 2);
  assert.equal(app.utterance(), null);
  journey.stop();
  app.coordinator.stop();
});

for (const [state, message] of [["recognized_single", "7011번 버스 인식 중."],
  ["matched_candidate", "7011번 버스 확인함."]]) {
  for (const [mode, options] of [["서버 MP3", {}], ["기기 음성 대체", { playFailure: true }],
    ["MP3 지원 없는 기기", { noAudio: true }], ["서버 지연 시 기기 음성", { deferred: true }]]) {
    test(`${mode}: ${state} 안내를 한 요청으로 정확히 두 번 읽으며 화면에는 한 번 표시한다`, () => {
      const app = harness(options);
      const journey = app.journey();
      const event = { status: "ready", captured_at_ms: 100000, event: { target_route: "7011",
        matches: [{ track_id: 2, route_number: "7011", state, token_score: .99 }] } };
      const text = `${message} ${message}`;
      app.coordinator.start();
      journey.start("7011");
      journey.accept(event);
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
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v2");
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
      assert.equal(app.audio().src, rate === 1 ? "/audio/walking-move-right-two.mp3?v=sesac-212-v2"
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
    assert.equal(app.connections.length, 0, "iOS 준비가 다른 플랫폼에서 그래프를 만들지 않는다");
    app.player.recordingStream();
    for (const rate of [1, 1.5, 2]) {
      app.player.setRate(rate);
      app.player.speak("멈추세요", 5000);
      assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=sesac-212-v2");
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
