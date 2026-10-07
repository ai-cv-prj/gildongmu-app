const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

// Transactions commit asynchronously; failed commits leave persisted records unchanged.
function deviceDatabase() {
  const records = new Map();
  const device = { failWrites: false, abortAfterSuccess: false, records, open() {
    const request = {};
    queueMicrotask(() => {
      request.result = { objectStoreNames: { contains: () => true }, close() {},
        transaction(_name, mode) {
          const transaction = { objectStore() {
            const execute = operation => {
              const result = {};
              queueMicrotask(() => {
                if (mode === "readwrite" && device.failWrites) {
                  transaction.error = new Error("QuotaExceededError");
                  transaction.onabort(); return;
                }
                if (mode === "readwrite" && device.abortAfterSuccess) {
                  result.onsuccess();
                  transaction.error = new Error("transaction aborted");
                  transaction.onabort(); return;
                }
                result.result = operation(); result.onsuccess(); transaction.oncomplete();
              });
              return result;
            };
            return {
              put(record) { return execute(() => records.set(record.key, structuredClone(record))); },
              getAll() { return execute(() => structuredClone([...records.values()])); },
              get(key) { return execute(() => structuredClone(records.get(key))); },
              delete(key) { return execute(() => records.delete(key)); },
            };
          } };
          return transaction;
        } };
      request.onsuccess();
    });
    return request;
  } };
  return device;
}

function harness(device) {
  const downloads = [], blobs = [];
  const context = { window: { indexedDB: device },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:video"; }, revokeObjectURL() {} },
    document: { body: { appendChild() {} }, createElement() {
      return { click() { downloads.push({ filename: this.download, href: this.href }); }, remove() {} };
    } }, setTimeout: callback => { callback(); },
  };
  vm.runInNewContext(fs.readFileSync("frontend/js/clip-store.js", "utf8"), context);
  return { store: context.window.GClipStore.create(), downloads, blobs };
}
function clip(index = 1) {
  return { index, started_at_ms: 1700000000000, ended_at_ms: 1700000030000,
    blob: new Blob(["original video"], { type: "video/webm" }),
    frames: [{ frame_id: 17, image: new Blob(["jpeg"], { type: "image/jpeg" }),
      mask_png: "mask", overlay_png: "overlay" }] };
}

test("보관한 원본과 추론 프레임은 새 보관함 인스턴스에서도 복원된다", async () => {
  const device = deviceDatabase(), first = harness(device);
  assert.equal((await first.store.save("session-1", clip())).stored, true);
  const reloaded = harness(device), records = await reloaded.store.list();
  assert.equal(records.length, 1);
  assert.equal(records[0].durable, true);
  assert.equal(await records[0].clip.blob.text(), "original video");
  assert.equal(await records[0].clip.frames[0].image.text(), "jpeg");
  assert.equal(records[0].clip.frames[0].overlay_png, "overlay");
  await reloaded.store.download(records[0].key);
  assert.match(reloaded.downloads[0].filename, /session-1-clip-1\.webm$/);
  assert.equal(await reloaded.blobs[0].text(), "original video");
});

test("저장 용량 부족 시 영상을 잃지 않고 현재 페이지에서 다운로드한다", async () => {
  const device = deviceDatabase(); device.failWrites = true;
  const h = harness(device), result = await h.store.save("session-1", clip());
  assert.equal(result.stored, false);
  assert.match(result.error, /QuotaExceeded/);
  const records = await h.store.list();
  assert.equal(records[0].durable, false);
  await h.store.download(records[0].key);
  assert.equal(await h.blobs[0].text(), "original video");
  assert.equal((await harness(device).store.list()).length, 0);
});

test("put 성공 뒤 트랜잭션이 취소되면 영구 저장 성공으로 표시하지 않는다", async () => {
  const device = deviceDatabase(); device.abortAfterSuccess = true;
  const h = harness(device);
  assert.equal((await h.store.save("session-1", clip())).stored, false);
  assert.equal(device.records.size, 0);
  assert.equal((await h.store.list())[0].durable, false);
});

test("서버 저장 완료로 삭제할 때 선택한 세션과 구간만 제거한다", async () => {
  const device = deviceDatabase(), h = harness(device);
  await h.store.save("session-1", clip(1));
  await h.store.save("session-1", clip(2));
  await h.store.save("session-2", clip(1));
  await h.store.remove("session-1", 1);
  assert.deepEqual(Array.from(await h.store.list(), r => r.key).sort(), ["session-1:2", "session-2:1"]);
});
