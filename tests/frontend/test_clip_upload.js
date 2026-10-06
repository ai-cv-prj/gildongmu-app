const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function apiWithRequests(requests, failFirst = false) {
  const attempts = new Map();
  const context = {
    Blob, FormData, setTimeout: callback => callback(),
    fetch: async (path, options) => {
      requests.push({ path, options });
      const count = attempts.get(path) || 0;
      attempts.set(path, count + 1);
      const fail = failFirst && count === 0 && path.endsWith("/chunks/0");
      return { ok: !fail, status: fail ? 503 : 200,
        json: async () => fail ? { detail: "일시적 장애" } : { state: "pending" } };
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/api.js", "utf8"), context);
  return context.GApi;
}

test("원본 청크와 추론에 쓴 동일 JPEG를 종료 후 순서대로 전송한다", async () => {
  const requests = [];
  const api = apiWithRequests(requests);
  const original = new Blob([new Uint8Array(4 * 1024 * 1024 + 8)], { type: "video/webm" });
  const image = new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" });
  const progress = [];
  await api.uploadClip("session-1", { index: 2, blob: original, started_at_ms: 100,
    ended_at_ms: 30100, frames: [{ frame_id: 17, captured_at_ms: 200,
      image, mask_png: "cG5n", overlay_png: "b3ZlcmxheQ==" }] },
    (done, total) => progress.push([done, total]));
  assert.deepEqual(requests.map(request => request.path), [
    "/api/sessions/session-1/clips/2/chunks/0",
    "/api/sessions/session-1/clips/2/chunks/1",
    "/api/sessions/session-1/clips/2/frames/17",
    "/api/sessions/session-1/clips/2/complete",
  ]);
  assert.equal(requests[0].options.body.get("video").size, 4 * 1024 * 1024);
  assert.equal(requests[1].options.body.get("video").size, 8);
  assert.equal(requests[2].options.body.get("image").size, image.size);
  assert.equal(requests[2].options.body.get("captured_at_ms"), "200");
  assert.equal(requests[2].options.body.get("mask_png"), "cG5n");
  assert.equal(requests[2].options.body.get("overlay_png"), "b3ZlcmxheQ==");
  assert.deepEqual(JSON.parse(requests[3].options.body), { mime_type: "video/webm",
    chunk_count: 2, size_bytes: original.size, started_at_ms: 100, ended_at_ms: 30100 });
  assert.deepEqual(progress, [[1, 4], [2, 4], [3, 4], [4, 4]]);
});

test("일시적 청크 업로드 실패만 재시도한다", async () => {
  const requests = [];
  const api = apiWithRequests(requests, true);
  await api.uploadClip("session-1", { index: 1, blob: new Blob(["video"], { type: "video/webm" }),
    started_at_ms: 100, ended_at_ms: 1000, frames: [] });
  assert.deepEqual(requests.map(request => request.path), [
    "/api/sessions/session-1/clips/1/chunks/0",
    "/api/sessions/session-1/clips/1/chunks/0",
    "/api/sessions/session-1/clips/1/complete",
  ]);
});
