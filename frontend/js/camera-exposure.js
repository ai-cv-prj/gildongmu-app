/** Android bus-only exposure trial. exposureTime uses 100 microsecond units. */
window.GCameraExposure = (() => {
  "use strict";
  const copy = value => JSON.parse(JSON.stringify(value));
  const abort = () => Object.assign(new Error("카메라 설정 작업 종료"), { name: "AbortError" });
  const fields = ["exposureMode", "exposureTime", "exposureCompensation", "iso", "frameRate", "width", "height"];

  function create(track, { enabled = true, exposureTimeUs = 16667, android = false,
    onChange = () => {}, onFatal = () => {}, timeoutMs = 3000, verifyMs = 800 } = {}) {
    let disposed = false, desired = false, revision = 0, baseline = null;
    let tail = Promise.resolve(), report = { status: "default", requested: false };
    const cancellations = new Set();
    const live = () => !disposed && track.readyState !== "ended";
    function settings() {
      try {
        const raw = track.getSettings?.() || {};
        return Object.fromEntries(fields.filter(key => raw[key] !== undefined).map(key => [key, raw[key]]));
      } catch (_) { return {}; }
    }
    function publish(status, extra = {}) {
      if (!live()) return;
      const current = settings();
      report = { status, requested: desired, target_us: exposureTimeUs,
        actual_us: Number.isFinite(current.exposureTime) ? current.exposureTime * 100 : null,
        settings: current, ...extra };
      onChange(copy(report));
    }
    function bounded(promise, duration = timeoutMs) {
      return new Promise((resolve, reject) => {
        let timer;
        const finish = (fn, value) => { clearTimeout(timer); cancellations.delete(cancel); fn(value); };
        const cancel = () => finish(reject, abort());
        cancellations.add(cancel);
        timer = setTimeout(() => finish(reject,
          Object.assign(new Error("카메라 설정 응답 시간 초과"), { name: "TimeoutError" })), duration);
        Promise.resolve(promise).then(value => finish(resolve, value), error => finish(reject, error));
        if (!live()) cancel();
      });
    }
    async function delay() {
      let timer;
      try { await bounded(new Promise(resolve => { timer = setTimeout(resolve, 50); })); }
      finally { clearTimeout(timer); }
    }
    async function verify(matches) {
      for (let elapsed = 0; elapsed <= verifyMs; elapsed += 50) {
        if (!live()) throw abort();
        if (matches(settings())) return true;
        if (elapsed < verifyMs) await delay();
      }
      return false;
    }
    // Preserve camera resolution, FPS, focus, zoom and any unrelated constraints.
    function constraintsWith(values) {
      const constraints = copy(baseline.constraints);
      delete constraints.exposureMode;
      delete constraints.exposureTime;
      const advanced = (constraints.advanced || []).map(item => {
        const next = { ...item }; delete next.exposureMode; delete next.exposureTime; return next;
      }).filter(item => Object.keys(item).length);
      return { ...constraints, advanced: [...advanced, values] };
    }
    async function apply(constraints) {
      if (!live()) throw abort();
      await bounded(track.applyConstraints(constraints));
      if (!live()) throw abort();
    }
    function fatal(reason) {
      publish("restore_failed", { reason });
      // A timed-out native operation may still execute. Retire this track rather
      // than racing another operation or claiming that automatic exposure is back.
      dispose();
      onFatal(reason);
    }
    async function restore() {
      if (!baseline || !live()) return;
      publish("restoring");
      const previous = baseline;
      const values = { exposureMode: previous.settings.exposureMode };
      try {
        // Mode first: exposureTime only takes effect in manual mode.
        await apply(constraintsWith(values));
        if (values.exposureMode === "manual") {
          values.exposureTime = previous.settings.exposureTime;
          await apply(constraintsWith(values));
        }
        // Restore the caller's original constraints as well as the exposure mode.
        await apply(previous.constraints);
        const ok = await verify(current => current.exposureMode === values.exposureMode &&
          (values.exposureMode !== "manual" || Math.abs(current.exposureTime - values.exposureTime) <= previous.tolerance));
        if (!ok) throw new Error("이전 노출 설정 복원 확인 실패");
        baseline = null;
      } catch (error) {
        if (live()) fatal(error.message);
      }
    }
    async function enable(token) {
      if (baseline || !live()) return;
      if (!enabled || !android) return publish("unsupported", { reason: enabled ? "not_android" : "disabled" });
      if (!["getCapabilities", "getSettings", "getConstraints", "applyConstraints"].every(key => typeof track[key] === "function")) {
        return publish("unsupported", { reason: "missing_api" });
      }
      let caps, before, original;
      try { caps = track.getCapabilities(); before = settings(); original = copy(track.getConstraints()); }
      catch (_) { return publish("unsupported", { reason: "capabilities_unavailable" }); }
      const range = caps.exposureTime;
      const requested = exposureTimeUs / 100;
      if (!caps.exposureMode?.includes("manual") || !range ||
          !Number.isFinite(range.min) || !Number.isFinite(range.max) ||
          !Number.isFinite(requested) || requested <= 0 || requested < range.min || requested > range.max) {
        return publish("unsupported", { reason: "manual_exposure_unavailable" });
      }
      // Refuse to change a state that we cannot restore and verify later.
      if (!["continuous", "manual"].includes(before.exposureMode) ||
          !caps.exposureMode.includes(before.exposureMode) ||
          (before.exposureMode === "manual" && !(before.exposureTime > 0))) {
        return publish("unsupported", { reason: "baseline_unavailable" });
      }
      const step = Number.isFinite(range.step) && range.step > 0 ? range.step : 0;
      const target = step ? range.min + Math.round((requested - range.min) / step) * step : requested;
      if (target < range.min || target > range.max || Math.abs(target - requested) > requested * .05) {
        return publish("unsupported", { reason: "exposure_step_too_large" });
      }
      const tolerance = Math.max(.1, requested * .02);
      baseline = { constraints: original, settings: before, tolerance };
      publish("applying", { requested_time_us: target * 100 });
      try {
        await apply(constraintsWith({ exposureMode: "manual" }));
        if (!await verify(current => current.exposureMode === "manual")) throw new Error("수동 노출 미적용");
        if (token !== revision) return; // The queued disable restores the baseline.
        await apply(constraintsWith({ exposureMode: "manual", exposureTime: target }));
        if (!await verify(current => current.exposureMode === "manual" &&
            Number.isFinite(current.exposureTime) && Math.abs(current.exposureTime - target) <= tolerance)) {
          throw new Error("요청한 노출 시간 미적용");
        }
        if (token === revision) publish("applied", { requested_time_us: target * 100 });
      } catch (error) {
        if (!live()) return;
        if (error.name === "TimeoutError") return fatal(error.message);
        await restore();
        if (live() && token === revision) publish("failed", { reason: error.message });
      }
    }
    function setBusMode(value) {
      value = Boolean(value);
      if (!live() || value === desired) return settled();
      desired = value;
      const token = ++revision;
      tail = tail.then(async () => {
        if (!live() || token !== revision) return;
        if (value) {
          // A rapid disable/re-enable may have interrupted an earlier apply.
          if (baseline) await restore();
          if (live() && token === revision) await enable(token);
        } else {
          await restore();
          if (live() && token === revision) publish("default");
        }
      }).catch(error => { if (live()) fatal(error.message); });
      return settled();
    }
    async function settled() {
      let pending;
      do { pending = tail; await pending; } while (pending !== tail);
      return copy(report);
    }
    function dispose() {
      disposed = true; desired = false; revision++;
      for (const cancel of [...cancellations]) cancel();
    }
    publish("default");
    return { setBusMode, settled, dispose, snapshot: () => copy(report) };
  }
  return { create };
})();
