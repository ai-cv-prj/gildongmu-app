/** Bounded client diagnostics survive offline failures and a page reload. */
window.GDiagnostics = (() => {
  "use strict";
  const STORAGE_KEY = "gildongmu-client-events-v1", MAX_EVENTS = 100, BATCH_SIZE = 20;
  const HISTORY_KEY = "gildongmu-client-history-v1";
  const textLimits = {
    event_id: 80, type: 80, request_id: 80, server_request_id: 80, method: 10,
    path: 400, operation: 80, error_name: 80, error_message: 500, visibility_state: 20,
    reason: 200, stage: 40, screen: 40, upload_stage: 40, state: 40,
    connection_type: 40, effective_type: 40, user_agent: 300,
  };
  const numbers = new Set(["occurred_at_ms", "monotonic_clock_ms", "time_origin_ms", "http_status",
    "elapsed_ms", "attempt", "frame_id", "clip_id", "buffered_bytes", "clip_count", "last_frame_id"]);
  const booleans = new Set(["online", "retryable", "running", "starting", "stopping", "paused",
    "camera_active", "recording_active", "connection_lost"]);
  const sessions = new Set(["session_id", "pending_stop_session_id", "pending_check_session_id"]);
  const arrays = new Set(["pending_clip_ids", "expected_clip_ids", "server_clip_ids"]);
  let queue = [], contextProvider = null, inFlight = null, sequence = 0;
  let history = [], storageAvailable = true;
  const listeners = new Set();
  const isFailure = event => /(?:error|failed|failure|discarded)/.test(event.type || "")
    || event.type === "network_offline" || Boolean(event.error_message);
  const summary = () => ({ count: history.filter(isFailure).length,
    pending: queue.filter(isFailure).length, storageAvailable });
  function publish() { for (const listener of listeners) { try { listener(summary()); } catch (_) {} } }
  function bounded(events) {
    if (events.length <= MAX_EVENTS) return events;
    const firstFailure = events.find(isFailure), recent = events.slice(-(MAX_EVENTS - 1));
    return firstFailure && !recent.includes(firstFailure) ? [firstFailure, ...recent] : events.slice(-MAX_EVENTS);
  }

  function clean(fields) {
    const result = {};
    if (!fields || typeof fields !== "object") return result;
    for (const [key, value] of Object.entries(fields)) {
      if (sessions.has(key)) {
        if (value === null || typeof value === "string" && /^[0-9a-f]{32}$/.test(value)) result[key] = value;
      } else if (textLimits[key] && typeof value === "string") {
        let text = key === "path" ? value.split(/[?#]/, 1)[0] : value;
        // Browser error messages can include URLs; do not persist their query values.
        if (key === "error_message") text = text.replace(/((?:https?:\/\/|\/)[^\s?#]*)[?#][^\s]*/g, "$1[redacted]");
        result[key] = text.slice(0, textLimits[key]);
      } else if (numbers.has(key) && Number.isFinite(value) && value >= 0 && Number.isSafeInteger(Math.round(value))) result[key] = Math.round(value);
      else if (booleans.has(key) && typeof value === "boolean") result[key] = value;
      else if (arrays.has(key) && Array.isArray(value)) result[key] = value.filter(item => Number.isSafeInteger(item) && item > 0).slice(0, 5);
    }
    return result;
  }
  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(queue));
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
      storageAvailable = true;
    } catch (_) { storageAvailable = false; }
    publish();
  }
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) queue = saved.map(clean).filter(event =>
      /^[a-zA-Z0-9_-]{1,80}$/.test(event.event_id || "") &&
      /^[a-zA-Z0-9_-]{1,80}$/.test(event.type || "") && event.occurred_at_ms > 0).slice(-MAX_EVENTS);
  } catch (_) { storageAvailable = false; }
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    history = bounded((Array.isArray(saved) ? saved : []).map(clean).filter(event =>
      event.event_id && event.type && event.occurred_at_ms > 0));
    const known = new Set(history.map(event => event.event_id));
    history = bounded([...history, ...queue.filter(event => !known.has(event.event_id))]);
  } catch (_) { history = [...queue]; storageAvailable = false; }

  function record(type, fields = {}) {
    try {
      let context = {};
      try { context = contextProvider?.() || {}; } catch (_) {}
      const connection = navigator.connection;
      const event = clean({ ...context, ...fields,
        event_id: globalThis.crypto?.randomUUID?.() ||
          `${Date.now().toString(36)}-${(++sequence).toString(36)}-${Math.random().toString(36).slice(2)}`,
        type: String(type).replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80) || "unknown",
        occurred_at_ms: Date.now(), monotonic_clock_ms: globalThis.performance?.now?.(),
        time_origin_ms: globalThis.performance?.timeOrigin,
        online: navigator.onLine, visibility_state: document.visibilityState,
        connection_type: connection?.type, effective_type: connection?.effectiveType,
        user_agent: navigator.userAgent,
      });
      queue = bounded([...queue, event]);
      history = bounded([...history, event]);
      persist();
    } catch (_) { /* Reporting is best effort and never breaks the app. */ }
  }
  function flush({ keepalive = false } = {}) {
    if (inFlight || !queue.length || navigator.onLine === false) return inFlight || Promise.resolve(false);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    inFlight = Promise.resolve().then(async () => {
      try {
        for (let sent = 0; sent < MAX_EVENTS / BATCH_SIZE && queue.length; sent++) {
          if (navigator.onLine === false || controller.signal.aborted) return false;
          const batch = queue.slice(0, BATCH_SIZE);
          let body = JSON.stringify({ events: batch });
          while (new Blob([body]).size > 60000 && batch.length > 1) {
            batch.pop(); body = JSON.stringify({ events: batch });
          }
          // This deliberately bypasses GApi so reporting failures cannot report themselves.
          const response = await fetch("/api/client-events", { method: "POST",
            headers: { "Content-Type": "application/json" }, body, keepalive, signal: controller.signal });
          if (!response.ok) return false;
          const result = await response.json();
          if (result.accepted !== batch.length) return false;
          const accepted = new Set(batch.map(event => event.event_id));
          queue = queue.filter(event => !accepted.has(event.event_id));
          persist();
        }
        return true;
      } catch (_) { return false; }
    }).finally(() => { clearTimeout(timeout); inFlight = null; });
    return inFlight;
  }
  window.addEventListener("online", () => { record("network_online"); void flush(); });
  window.addEventListener("offline", () => record("network_offline"));
  window.addEventListener("pagehide", () => { record("page_hidden"); void flush({ keepalive: true }); });
  document.addEventListener("visibilitychange", () => {
    record("visibility_changed");
    void flush({ keepalive: document.visibilityState === "hidden" });
  });
  setInterval(() => { void flush(); }, 15000);
  // A prior failure is retried even if this page has not started a new session yet.
  void flush();
  function exportData() {
    const pending = new Set(queue.map(event => event.event_id));
    return { format_version: 1, exported_at_ms: Date.now(),
      events: history.map(event => ({ ...event, delivered: !pending.has(event.event_id) })),
      fetch_errors: window.GFetchDiagnostics?.exportData?.() || null };
  }
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(exportData(), null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = `gildongmu-errors-${Date.now()}.json`;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return { record, flush, summary, exportData, download,
    subscribe(listener) { listeners.add(listener); listener(summary()); },
    setContext(provider) { contextProvider = typeof provider === "function" ? provider : null; } };
})();
