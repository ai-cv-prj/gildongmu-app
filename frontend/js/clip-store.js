/** Keep original clips and inference frames on the device until server output is ready. */
(() => {
  "use strict";
  function create({ indexedDB = window.indexedDB, now = () => Date.now() } = {}) {
    let database = null;
    const memory = new Map();
    const keyFor = (sessionId, clipId) => `${sessionId}:${clipId}`;

    function open() {
      if (database) return database;
      database = new Promise((resolve, reject) => {
        if (!indexedDB) return reject(new Error("이 브라우저는 영상의 기기 보관을 지원하지 않습니다."));
        const request = indexedDB.open("gildongmu-clips-v1", 1);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains("clips")) {
            request.result.createObjectStore("clips", { keyPath: "key" });
          }
        };
        request.onerror = () => reject(request.error || new Error("기기 영상 보관함을 열 수 없습니다."));
        request.onblocked = () => reject(new Error("다른 창에서 영상 보관함을 사용하고 있습니다."));
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => { db.close(); database = null; };
          resolve(db);
        };
      }).catch(error => { database = null; throw error; });
      return database;
    }

    async function transact(mode, operation) {
      const db = await open();
      return new Promise((resolve, reject) => {
        let transaction, result;
        try {
          transaction = db.transaction("clips", mode);
          const request = operation(transaction.objectStore("clips"));
          request.onsuccess = () => { result = request.result; };
          transaction.oncomplete = () => resolve(result);
          transaction.onabort = transaction.onerror = () => reject(
            transaction.error || request.error || new Error("기기에 영상을 보관하지 못했습니다."));
        } catch (error) { reject(error); }
      });
    }

    async function save(sessionId, clip) {
      if (!sessionId || !clip?.blob?.size) throw new Error("보관할 원본 영상이 없습니다.");
      const record = { key: keyFor(sessionId, clip.index), session_id: sessionId, saved_at_ms: now(),
        durable: false, clip: { ...clip, frames: (clip.frames || []).map(frame => ({ ...frame })) } };
      memory.set(record.key, record);
      try {
        await transact("readwrite", store => store.put({ ...record, durable: true }));
        record.durable = true;
        return { stored: true, key: record.key };
      } catch (error) {
        return { stored: false, key: record.key, error: error.message };
      }
    }

    async function list() {
      let records = [];
      try { records = await transact("readonly", store => store.getAll()); } catch (_) {}
      const merged = new Map((records || []).map(record => [record.key, record]));
      for (const [key, record] of memory) merged.set(key, record);
      return [...merged.values()].filter(record => record.clip?.blob?.size)
        .sort((a, b) => b.clip.started_at_ms - a.clip.started_at_ms);
    }

    async function remove(sessionId, clipId) {
      const key = keyFor(sessionId, clipId);
      await transact("readwrite", store => store.delete(key));
      memory.delete(key);
    }

    async function download(key) {
      const record = memory.get(key) || await transact("readonly", store => store.get(key));
      if (!record?.clip?.blob?.size) throw new Error("보관한 영상을 찾을 수 없습니다.");
      const extension = record.clip.blob.type.includes("mp4") ? "mp4" : "webm";
      const url = URL.createObjectURL(record.clip.blob), link = document.createElement("a");
      link.href = url;
      link.download = `gildongmu-${record.session_id}-clip-${record.clip.index}.${extension}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    return { save, list, remove, download };
  }
  window.GClipStore = { create };
})();
