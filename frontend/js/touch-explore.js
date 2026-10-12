/** Read a control on direct touch; activate its original click on a second tap. */
(function (global) {
  "use strict";
  const DOUBLE_TAP_MS = 500;
  const COOLDOWN_MS = 350;
  const CLICK_LIFETIME_MS = 1000;
  const MOVE_LIMIT_PX = 12;
  const clean = text => String(text || "").replace(/\s+/g, " ").trim();

  function create({ root = global.document, onSpeak = () => {}, onActivate = () => {},
    now = () => performance.now() } = {}) {
    const doc = root.ownerDocument || root;
    const pointers = new Map(), completedPointers = new Map(), listeners = [];
    let selected = null, pendingClick = null, cooldownUntil = -Infinity, destroyed = false;
    let usageExplained = false;

    function listen(type, callback) {
      root.addEventListener(type, callback, true);
      listeners.push(() => root.removeEventListener(type, callback, true));
    }
    function clearSelection() {
      selected?.control.classList.remove("touch-selected");
      selected = null;
    }
    function reset() {
      clearSelection();
      for (const pointer of pointers.values()) pointer.cancelled = true;
      for (const pointer of completedPointers.values()) pointer.cancelled = true;
      if (pendingClick) pendingClick.cancelled = true;
      // Navigation can reset the view inside the accepted click. Keep its guard
      // so the third finger tap cannot select or activate the next screen.
    }
    function usable(control) {
      if (!control || !root.contains(control) || control.disabled || control.matches?.(":disabled")) return false;
      for (let element = control; element; element = element.parentElement) {
        if (element.hidden || element.inert || element.getAttribute("aria-hidden") === "true"
          || element.getAttribute("aria-disabled") === "true") return false;
        const style = doc.defaultView?.getComputedStyle?.(element);
        if (style?.display === "none" || style?.visibility === "hidden" || style?.visibility === "collapse") return false;
      }
      return true;
    }
    function controlAt(target) {
      const element = target?.closest ? target : target?.parentElement;
      // Native editing and links nested inside a disclosure keep their own behavior.
      if (element?.closest("input, select, textarea, a[href], [contenteditable]:not([contenteditable='false'])")) return null;
      const control = element?.closest("button, summary");
      return usable(control) ? control : null;
    }
    function nameOf(control) {
      const labelledBy = clean(control.getAttribute("aria-labelledby")).split(" ").filter(Boolean)
        .map(id => clean(doc.getElementById(id)?.textContent)).filter(Boolean).join(" ");
      let label = labelledBy || clean(control.getAttribute("aria-label")) || clean(control.textContent)
        || clean(control.getAttribute("title")) || "버튼";
      if (control.dataset.rate !== undefined) label = `음성 속도 ${control.dataset.rate}배`;
      else if (control.dataset.size !== undefined) label = `글자 크기 ${label}`;
      return label;
    }
    function stateOf(control) {
      const states = ["aria-pressed", "aria-selected", "aria-checked", "aria-expanded"]
        .map(attribute => control.getAttribute(attribute));
      if (control.tagName === "SUMMARY") states.push(String(Boolean(control.parentElement?.open)));
      return states;
    }
    function signature(control) {
      return JSON.stringify([nameOf(control), stateOf(control), control.type,
        Object.keys(control.dataset).sort().map(key => [key, control.dataset[key]])]);
    }
    function announce(control) {
      let label = nameOf(control);
      if (["aria-pressed", "aria-selected", "aria-checked"].some(attribute => control.getAttribute(attribute) === "true")) {
        label += ", 선택됨";
      }
      if (control.tagName === "SUMMARY") label += control.parentElement?.open ? ", 펼쳐짐" : ", 접힘";
      let message = `${label}.`;
      if (control.dataset.action === "enter" && !usageExplained) {
        const help = clean(doc.getElementById("touch-help")?.textContent);
        if (help) {
          usageExplained = true;
          message += ` ${help}`;
        }
      }
      onSpeak(message);
    }
    function isTouch(event) { return event.pointerType === "touch" || event.pointerType === "pen"; }
    function moved(pointer, event) {
      return Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > MOVE_LIMIT_PX;
    }
    function pointerDown(event) {
      if (!isTouch(event)) {
        reset(); pendingClick = null; completedPointers.clear(); return;
      }
      const time = now();
      for (const [id, pointer] of completedPointers) {
        if (time - pointer.endedAt > CLICK_LIFETIME_MS) completedPointers.delete(id);
      }
      // Backgrounding can swallow pointerup/cancel. A new primary touch means
      // the prior physical sequence ended; otherwise its stale entry would
      // make every later tap look like a second finger. Keep cancelled click
      // provenance in case the old browser click arrives after foregrounding.
      if (event.isPrimary === true && pointers.size) {
        for (const [id, pointer] of pointers) completedPointers.set(id, { ...pointer, cancelled: true, endedAt: time });
        pointers.clear(); clearSelection();
      }
      completedPointers.delete(event.pointerId);
      const control = controlAt(event.target);
      const multiple = pointers.size > 0 || event.isPrimary === false;
      if (multiple) reset();
      if (!control || selected?.control !== control) clearSelection();
      pendingClick = null;
      pointers.set(event.pointerId, { control, pointerId: event.pointerId, pointerType: event.pointerType,
        x: event.clientX, y: event.clientY, signature: control ? signature(control) : null,
        cancelled: multiple, suppressed: time < cooldownUntil });
    }
    function pointerMove(event) {
      const pointer = pointers.get(event.pointerId);
      if (pointer && moved(pointer, event)) { pointer.cancelled = true; clearSelection(); }
    }
    function pointerEnd(event) {
      const pointer = pointers.get(event.pointerId);
      if (!pointer) return;
      pointers.delete(event.pointerId);
      pointer.cancelled ||= event.type === "pointercancel" || moved(pointer, event)
        || controlAt(event.target) !== pointer.control;
      if (pointer.cancelled) clearSelection();
      const element = event.target?.closest ? event.target : event.target?.parentElement;
      pendingClick = { ...pointer, releaseControl: element?.closest("button, summary"), endedAt: now() };
      completedPointers.set(event.pointerId, pendingClick);
    }
    function stop(event) { event.preventDefault(); event.stopImmediatePropagation(); }
    function click(event) {
      const control = controlAt(event.target);
      const element = event.target?.closest ? event.target : event.target?.parentElement;
      const clickedControl = element?.closest("button, summary");
      const time = now();
      const fresh = pointer => pointer && time >= pointer.endedAt && time - pointer.endedAt <= CLICK_LIFETIME_MS;
      const matches = pointer => clickedControl && (pointer.control === clickedControl || pointer.releaseControl === clickedControl);
      const exact = completedPointers.get(event.pointerId);
      let pointer = fresh(exact) && matches(exact) && event.pointerType === exact.pointerType ? exact : null;
      if (!pointer) {
        // iOS Safari can emit click(type=mouse, id=1) after a real touch with
        // another ID (WebKit #282988). Correlate the raw touch and its target,
        // not the compatibility click's device metadata. Cancelled touches
        // remain candidates so their late clicks stay blocked as well.
        for (const candidate of completedPointers.values()) {
          if (fresh(candidate) && matches(candidate) && (!pointer || candidate.endedAt >= pointer.endedAt)) pointer = candidate;
        }
        if (!pointer && fresh(pendingClick) && matches(pendingClick)) pointer = pendingClick;
        if (!pointer && fresh(exact)) pointer = exact;
      }
      if (pointer) completedPointers.delete(pointer.pointerId);
      if (event.detail === 0 || pendingClick === pointer) pendingClick = null;
      // Browsers expose no reliable screen-reader-enabled flag. Require an
      // actual touch/pen sequence, and leave semantic (detail=0), keyboard and
      // mouse activation alone. This is an input heuristic, not AT detection.
      const directTouch = event.detail !== 0 && fresh(pointer);
      if (!control) {
        clearSelection();
        // A control disabled/hidden between down and click must never fire.
        if (directTouch && pointer.control) stop(event);
        return;
      }
      if (!directTouch) {
        pendingClick = null; completedPointers.clear();
        clearSelection(); onActivate(); return;
      }
      if (pointer.cancelled || pointer.suppressed || now() < cooldownUntil || pointer.control !== control
        || pointer.signature !== signature(control)) {
        stop(event); clearSelection(); return;
      }
      const currentSignature = signature(control);
      if (selected?.control === control && selected.signature === currentSignature
        && time >= selected.at && time - selected.at <= DOUBLE_TAP_MS) {
        clearSelection();
        cooldownUntil = time + COOLDOWN_MS;
        onActivate();
        // Preserve the trusted event and active user gesture for camera/audio,
        // form submission, file pickers, and the existing button click handler.
        return;
      }
      stop(event);
      clearSelection();
      selected = { control, signature: currentSignature, at: time };
      control.classList.add("touch-selected");
      announce(control);
    }
    listen("pointerdown", pointerDown);
    listen("pointermove", pointerMove);
    listen("pointerup", pointerEnd);
    listen("pointercancel", pointerEnd);
    listen("click", click);
    listen("scroll", reset);
    listen("visibilitychange", () => { if (doc.hidden) reset(); });
    return { reset, destroy() {
      if (destroyed) return;
      destroyed = true;
      reset(); pointers.clear(); completedPointers.clear(); pendingClick = null;
      listeners.forEach(remove => remove());
    } };
  }
  const api = { create };
  global.GTouchExplore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
