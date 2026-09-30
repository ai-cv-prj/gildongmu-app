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
  const context = { window: {}, fetch: async (url, options) => {
    assert.equal(url, "/api/config");
    assert.equal(options.cache, "no-store");
    return { ok, status: 503, json: async () => defaults };
  } };
  vm.runInNewContext(fs.readFileSync("frontend/js/config.js", "utf8"), context);
  assert.throws(() => context.window.GConfig.get(), /준비/);
  await context.window.GConfig.load();
  assert.equal(context.window.GConfig.get().recording.fps, defaults.recording.fps);
  ok = false;
  await assert.rejects(context.window.GConfig.load(), /503/);
});

// 기본값과 다른 공개 설정으로 실제 녹화 API 호출 인자를 확인한다.
test("recorder uses server fps, size, bitrate and chunk interval", () => {
  const settings = structuredClone(defaults);
  Object.assign(settings.recording, { fps: 7, max_side: 320,
    video_bits_per_second: 1234567, chunk_interval_ms: 345 });
  const calls = {};
  const canvas = { getContext: () => ({ fillRect() {}, drawImage() {} }),
    captureStream(fps) { calls.fps = fps; return {}; } };
  const video = { videoWidth: 1280, videoHeight: 720 };
  const overlay = { clientWidth: 1280, clientHeight: 720, width: 1280, height: 720 };
  class Recorder {
    // 브라우저 녹화 생성 인자 기록
    /** 실제 미디어 장치 없이 선택한 비트레이트를 기록한다. */
    constructor(_stream, options) { calls.options = options; this.state = "inactive"; }
    // 브라우저 MIME 지원 모사
    /** 테스트에서는 기본 WebM 지원 여부를 참으로 반환한다. */
    static isTypeSupported() { return true; }
    // 녹화 데이터 수집 간격 기록
    /** 실제 녹화 타이머 없이 설정된 청크 간격을 기록한다. */
    start(interval) { calls.interval = interval; }
  }
  const context = { window: { GConfig: { get: () => settings }, MediaRecorder: Recorder },
    MediaRecorder: Recorder, performance: { now: () => 0 },
    document: { getElementById: id => id === "video" ? video : overlay, createElement: () => canvas } };
  vm.runInNewContext(fs.readFileSync("frontend/js/recorder.js", "utf8"), context);
  context.window.GRecorder.start();
  assert.equal(calls.fps, 7);
  assert.equal(canvas.width, 320);
  assert.equal(canvas.height, 180);
  assert.equal(calls.options.videoBitsPerSecond, 1234567);
  assert.equal(calls.interval, 345);
});
