/**
 * file_path: tests/frontend/test_config.js
 *
 * 설정 준비·실패 처리와 변경한 녹화 설정의 브라우저 적용을 확인한다.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");
const defaults = require("./settings");

// 서버 설정을 읽기 전에는 사용하지 못하고 캐시 없이 조회한다.
test("configuration loads before use and reports request failures", async () => {
  let ok = true;
  const events = [];
  const context = { window: { GDiagnostics: { record(type, fields) { events.push({ type, ...fields }); } } },
    AbortController, clearTimeout,
    setTimeout: (callback, delay) => setTimeout(callback, delay <= 1000 ? 0 : delay),
    fetch: async (url, options) => {
    assert.equal(url, "/api/config");
    assert.equal(options.cache, "no-store");
    return { ok, status: 503, json: async () => defaults };
  } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/api.js", "utf8"), context);
  vm.runInContext(fs.readFileSync("frontend/js/config.js", "utf8"), context);
  assert.throws(() => context.window.GConfig.get(), /준비/);
  await context.window.GConfig.load();
  assert.equal(context.window.GConfig.get().recording.fps, defaults.recording.fps);
  ok = false;
  await assert.rejects(context.window.GConfig.load(), /503/);
  assert.equal(events.filter(event => event.type === "request_error").length, 3);
  assert.ok(events.every(event => event.operation === "config"));
});

// 기본값과 다른 공개 설정으로 화면 갱신과 선택형 원본 녹화 인자를 확인한다.
test("preview fps and optional raw recorder use server settings", async () => {
  const settings = structuredClone(defaults);
  Object.assign(settings.recording, { fps: 7, max_side: 320,
    video_bits_per_second: 1234567, chunk_interval_ms: 345 });
  const calls = { draws: 0 };
  const canvas = { getContext: () => ({ fillRect() {}, clearRect() {},
    drawImage() { calls.draws++; } }),
    captureStream() { throw new Error("저장용 오버레이 인코더를 시작하면 안 됩니다."); } };
  const track = { readyState: "live" };
  const video = { videoWidth: 1280, videoHeight: 720,
    srcObject: { getVideoTracks: () => [track] } };
  const overlay = { clientWidth: 1280, clientHeight: 720, width: 1280, height: 720 };
  class Recorder {
    // 브라우저 녹화 생성 인자 기록
    /** 실제 미디어 장치 없이 선택한 비트레이트를 기록한다. */
    constructor(_stream, options) { calls.options = options; calls.recorder = this; this.state = "inactive"; }
    // 브라우저 MIME 지원 모사
    /** 테스트에서는 기본 WebM 지원 여부를 참으로 반환한다. */
    static isTypeSupported() { return true; }
    // 녹화 데이터 수집 간격 기록
    /** 실제 녹화 타이머 없이 설정된 청크 간격을 기록한다. */
    start(interval) { calls.interval = interval; this.state = "recording"; }
    stop() { this.state = "inactive";
      this.ondataavailable({ data: new Blob(["raw"], { type: "video/webm" }) });
      this.onstop(); }
  }
  let draw, now = 0;
  const context = { window: { GConfig: { get: () => settings }, MediaRecorder: Recorder },
    MediaRecorder: Recorder, MediaStream: class { constructor(tracks) { calls.tracks = tracks; } },
    Blob, performance: { now: () => now, timeOrigin: 100000 },
    requestAnimationFrame: callback => { draw = callback; return 1; }, cancelAnimationFrame() {},
    document: { getElementById: id => id === "video" ? video : id === "overlay" ? overlay : canvas } };
  vm.runInNewContext(fs.readFileSync("frontend/js/recorder.js", "utf8"), context);
  context.window.GRecorder.startPreview();
  assert.equal(calls.draws, 2);
  draw(100);
  assert.equal(calls.draws, 2, "설정 FPS보다 빠른 화면 갱신은 건너뛴다");
  draw(145);
  assert.equal(calls.draws, 4, "설정 FPS 간격에 새 화면을 그린다");
  assert.equal(canvas.width, 320);
  assert.equal(canvas.height, 180);
  now = 145;
  assert.equal(context.window.GRecorder.startRaw(), 100145);
  assert.equal(calls.tracks.length, 1);
  assert.strictEqual(calls.tracks[0], track);
  assert.equal(calls.options.videoBitsPerSecond, 1234567);
  assert.equal(calls.interval, 345);
  const saved = await context.window.GRecorder.stopRaw();
  assert.equal(saved.blob.size, 3);
  const audioTrack = { readyState: "live" };
  context.window.GRecorder.startRaw(null, null, { getAudioTracks: () => [audioTrack] });
  assert.equal(calls.tracks.length, 2);
  assert.strictEqual(calls.tracks[1], audioTrack);
  await context.window.GRecorder.stopRaw();
  context.window.GRecorder.startRaw(() => false);
  calls.recorder.ondataavailable({ data: new Blob(["overflow"], { type: "video/webm" }) });
  await assert.rejects(context.window.GRecorder.stopRaw(), /120MB/,
    "용량 상한을 넘기는 청크는 누적하지 않고 기록을 끝낸다");
});
