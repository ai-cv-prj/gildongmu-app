const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness({ noAudio = false, playFailure = false, deferred = false } = {}) {
  let audio = null, rejectPlayback = null, utterance = null, cancels = 0;
  const audioSources = [], connections = [], timers = new Map();
  let timerId = 0;
  class Audio {
    constructor() { audio = this; }
    pause() {}
    load() {}
    removeAttribute(name) { delete this[name]; }
    play() {
      if (deferred && this.src.startsWith("/api")) return new Promise((_, reject) => { rejectPlayback = reject; });
      if (playFailure) throw Object.assign(new Error("Missing MP3"), { name: "NotSupportedError" });
      this.onplaying?.();
    }
  }
  const settings = { audio: { volume: 1, playback_rate: 1.5, default_validity_ms: 8000,
    playback_timeout_ms: 15000, crosswalk_max_age_ms: 1500 } };
  const context = { window: { GConfig: { get: () => settings }, Audio: noAudio ? undefined : Audio,
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
    speechSynthesis: { getVoices: () => [{ lang: "ko-KR" }], speak(value) { utterance = value; value.onstart(); },
      cancel() { cancels++; } },
  }, performance: { now: () => 0 },
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); } };
  vm.createContext(context);
  for (const name of ["tts", "audio_coordinator"]) {
    vm.runInContext(fs.readFileSync(`frontend/js/${name}.js`, "utf8"), context);
  }
  const player = context.window.GTts.create();
  const coordinator = context.window.GAudioCoordinator.create({ player });
  return { player, coordinator, audio: () => audio, utterance: () => utterance, cancels: () => cancels,
    audioSources, connections, timers,
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
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=walking-action-v9");
  assert.equal(app.audio().playbackRate, .75);
  app.player.cancel();
});

test("동적 MP3 재생 실패는 한국어 브라우저 음성으로 이어지고 취소 후 완료 콜백은 무시한다", () => {
  const app = harness({ playFailure: true });
  let completed = 0;
  app.player.setRate(1.2);
  assert.equal(app.player.speak("143번 버스 번호를 확인했습니다.", 5000,
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
  assert.equal(app.player.speak("143번 버스 번호를 확인했습니다.", 5000, { dynamic: true }), true);
  assert.match(app.utterance().text, /143번/);
  app.player.cancel();
  assert.equal(app.player.recordingStream(), null);
});

test("긴급 음성이 동적 MP3를 취소한 뒤 늦은 MP3 오류가 버스 합성을 되살리지 않는다", async () => {
  const app = harness({ deferred: true });
  app.coordinator.start();
  app.coordinator.request({ source: "bus-ocr", priority: app.coordinator.PRIORITY.busOcr,
    dynamic: true, text: "143번으로 보이는 버스가 있습니다.", validUntil: 3000 });
  assert.ok(app.audio().src.startsWith("/api/bus-arrival-speech"));
  app.coordinator.request({ source: "walking", priority: app.coordinator.PRIORITY.emergency,
    text: "멈추세요.", validUntil: 3000 });
  app.rejectAudio();
  await Promise.resolve();
  assert.equal(app.utterance(), null);
  assert.equal(app.audio().src, "/audio/walking-stop.mp3?v=walking-action-v9");
  app.coordinator.stop();
});
