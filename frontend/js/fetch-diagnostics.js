/** Small durable fetch-error history; transport failures never depend on video uploads. */
(() => {
  "use strict";
  const KEY = "gildongmu-fetch-errors-v1", MAX_RECORDS = 100;
  function create({ transport = window.fetch.bind(window), storage = null,
    now = () => Date.now(), monotonic = () => performance.now() } = {}) {
    let records = [], dropped = 0, sourceId = null, lastSuccess = null;
    let contextProvider = () => ({}), task = null, timer = null, storageAvailable = Boolean(storage);
    const listeners = new Set(), responses = new WeakMap();
    const id = () => window.crypto?.randomUUID?.() || `${now()}-${Math.random().toString(16).slice(2)}`;
    const short = (value, length) => typeof value === "string" ? value.slice(0, length) : null;
    const positive = value => Number.isInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
    try {
      const saved = JSON.parse(storage?.getItem(KEY) || "{}");
      records = (Array.isArray(saved.records) ? saved.records : []).filter(entry =>
        entry.record?.event_id && typeof entry.pending === "boolean").slice(-MAX_RECORDS);
      dropped = Number.isInteger(saved.dropped) ? saved.dropped : 0;
    } catch (_) { storageAvailable = false; }
    const summary = () => ({ count: records.length, pending: records.filter(entry => entry.pending).length,
      storageAvailable, dropped });
    function publish() { for (const listener of listeners) { try { listener(summary()); } catch (_) {} } }
    function save() {
      try { if (storage) { storage.setItem(KEY, JSON.stringify({ records, dropped })); storageAvailable = true; } }
      catch (_) { storageAvailable = false; }
      publish();
    }
    function configure(value) {
      if (!value) return;
      sourceId = short(value, 80);
      let changed = false;
      for (const entry of records) if (!entry.record.source_id) { entry.record.source_id = sourceId; changed = true; }
      if (changed) save();
    }
    function schedule() {
      if (timer !== null) return;
      timer = setTimeout(() => { timer = null; void flush(); }, 1000);
    }
    function report(error, info, phase, httpStatus = null) {
      if (error?.name === "AbortError") return;
      let context = {};
      try { context = contextProvider() || {}; } catch (_) {}
      const record = { event_id: id(), request_id: info.request_id, occurred_at_ms: now(),
        source_id: sourceId, session_id: info.session_id || short(context.session_id, 80),
        frame_id: info.frame_id, captured_at_ms: info.captured_at_ms, clip_id: info.clip_id,
        path: info.path, method: info.method, phase, attempt: info.attempt,
        error_name: short(error?.name || "Error", 80), message: short(String(error?.message || error), 500),
        http_status: httpStatus, duration_ms: Math.max(0, Math.round(monotonic() - info.started)),
        last_success_at_ms: lastSuccess, online: typeof navigator.onLine === "boolean" ? navigator.onLine : null,
        visibility: short(document.visibilityState, 20), screen: short(context.screen, 40),
        ui_action: short(context.ui_action, 40), running: context.running === true,
        recording_active: context.recording_active === true,
        pending_upload_count: Math.max(0, Math.min(100, Number(context.pending_upload_count) || 0)) };
      records.push({ record, pending: true });
      if (records.length > MAX_RECORDS) { records.shift(); dropped++; }
      save(); // Synchronous persistence happens before the app's failure/stop handler.
      schedule();
    }
    async function trackedFetch(input, options = {}, extra = {}) {
      const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
      if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")
          || url.pathname === "/api/client-errors") return transport(input, options);
      const session = url.pathname.match(/^\/api\/sessions\/([^/]+)(?:\/|$)/);
      let stopSession = null;
      if (url.pathname === "/api/sessions/stop") {
        try { stopSession = JSON.parse(options.body).session_id; } catch (_) {}
      }
      const info = { request_id: id(), started: monotonic(), path: url.pathname,
        method: (options.method || "GET").toUpperCase(), attempt: extra.attempt || 1,
        session_id: short(stopSession || (session?.[1] !== "stop" ? session?.[1] : null), 80),
        frame_id: positive(options.body?.get?.("frame_id")),
        captured_at_ms: positive(options.body?.get?.("captured_at_ms")),
        clip_id: positive(url.pathname.match(/\/clips\/(\d+)/)?.[1]) };
      const headers = new Headers(options.headers);
      headers.set("X-Client-Request-ID", info.request_id);
      try {
        const response = await transport(input, { ...options, headers });
        configure(response.headers?.get?.("X-Hub-Source-ID"));
        responses.set(response, info);
        return response;
      } catch (error) { report(error, info, "fetch"); throw error; }
    }
    async function json(response) {
      try {
        const body = await response.json();
        if (response.ok) { lastSuccess = now(); if (summary().pending) schedule(); }
        return body;
      } catch (error) {
        const info = responses.get(response);
        if (info && error?.name === "TypeError") report(error, info, "response_body", response.status);
        throw error;
      }
    }
    function flush() {
      if (task) return task;
      const pending = records.filter(entry => entry.pending).slice(0, 20);
      if (!pending.length || navigator.onLine === false) return Promise.resolve();
      task = (async () => {
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), 5000);
        try {
          // Raw transport prevents failed diagnostic delivery from recording itself recursively.
          const response = await transport("/api/client-errors", { method: "POST",
            headers: { "Content-Type": "application/json" }, signal: controller.signal,
            body: JSON.stringify({ records: pending.map(entry => entry.record) }) });
          if (!response.ok) return;
          const body = await response.json();
          if (!Array.isArray(body.saved_ids)) return;
          configure(response.headers?.get?.("X-Hub-Source-ID"));
          const acknowledged = new Set(body.saved_ids);
          let progress = false;
          for (const entry of pending) if (acknowledged.has(entry.record.event_id)) {
            entry.pending = false; progress = true;
          }
          save();
          if (progress && summary().pending) schedule();
        } catch (_) { /* Keep pending errors for recovery or a local download. */ }
        finally { clearTimeout(deadline); }
      })().finally(() => { task = null; });
      return task;
    }
    function exportData() {
      return { format_version: 1, exported_at_ms: now(), source_id: sourceId, dropped_records: dropped,
        records: records.map(entry => ({ ...entry.record, delivered: !entry.pending })) };
    }
    function download() {
      const url = URL.createObjectURL(new Blob([JSON.stringify(exportData(), null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url; link.download = `fetch-errors-${now()}.json`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    return { fetch: trackedFetch, json, flush, download, exportData, summary,
      setContextProvider(provider) { contextProvider = provider; },
      subscribe(listener) { listeners.add(listener); listener(summary()); } };
  }
  let storage = null;
  try { storage = window.localStorage; } catch (_) {}
  const diagnostics = create({ storage });
  diagnostics.create = create;
  window.GFetchDiagnostics = diagnostics;
  window.addEventListener("online", () => { void diagnostics.flush(); });
  setInterval(() => { void diagnostics.flush(); }, 15000);
})();
