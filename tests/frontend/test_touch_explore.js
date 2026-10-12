const assert = require("node:assert/strict");
const test = require("node:test");
const { create } = require("../../frontend/js/touch-explore.js");

function harness() {
  let time = 1000, nextPointer = 1;
  const spoken = [], actions = [], order = [];
  let activations = 0, submits = 0;
  class Element {
    constructor(tag = "div", label = "") {
      this.tagName = tag.toUpperCase(); this.textContent = label; this.dataset = {};
      this.attributes = new Map(); this.handlers = new Map(); this.children = [];
      this.parentElement = null; this.ownerDocument = null;
      this.disabled = false; this.hidden = false; this.inert = false; this.open = false;
      this.type = this.tagName === "BUTTON" ? "button" : undefined;
      const classes = new Set();
      this.classList = { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name) };
    }
    append(element) { element.parentElement = this; element.ownerDocument = doc; this.children.push(element); return element; }
    contains(element) { return this === element || this.children.some(child => child.contains(element)); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    matches(selector) { return selector === ":disabled" && (this.disabled || (this.parentElement?.tagName === "FIELDSET" && this.parentElement.disabled)); }
    closest(selector) {
      for (let element = this; element; element = element.parentElement) {
        if (selector === "button, summary" && ["BUTTON", "SUMMARY"].includes(element.tagName)) return element;
        if (selector.startsWith("input,") && (["INPUT", "SELECT", "TEXTAREA"].includes(element.tagName)
          || (element.tagName === "A" && element.getAttribute("href"))
          || (element.getAttribute("contenteditable") !== null && element.getAttribute("contenteditable") !== "false"))) return element;
      }
      return null;
    }
    addEventListener(type, callback, capture = false) {
      if (!this.handlers.has(type)) this.handlers.set(type, []);
      this.handlers.get(type).push({ callback, capture: Boolean(capture) });
    }
    removeEventListener(type, callback, capture = false) {
      this.handlers.set(type, (this.handlers.get(type) || []).filter(item => item.callback !== callback || item.capture !== Boolean(capture)));
    }
  }
  const doc = new Element("document"); doc.ownerDocument = null;
  doc.getElementById = id => {
    const visit = element => element.id === id ? element : element.children.map(visit).find(Boolean);
    return visit(doc);
  };
  const body = doc.append(new Element("main"));
  function add(label, options = {}) {
    const element = (options.parent || body).append(new Element(options.tag || "button", label));
    element.dataset = { ...(options.dataset || {}) }; element.type = options.type || element.type;
    for (const [name, value] of Object.entries(options.attrs || {})) element.setAttribute(name, value);
    if (element.tagName === "BUTTON") element.addEventListener("click", event => {
      actions.push({ label: element.textContent, event }); order.push("action"); options.onClick?.(event);
    });
    return element;
  }
  function dispatch(type, target, data = {}) {
    const event = { type, target, detail: 1, isTrusted: true, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...data };
    const path = []; for (let element = target; element; element = element.parentElement) path.push(element);
    for (const [nodes, capture] of [[path.slice().reverse(), true], [path, false]]) {
      for (const node of nodes) {
        for (const listener of node.handlers.get(type) || []) {
          if (listener.capture === capture) listener.callback(event);
          if (event.stopped) return event;
        }
      }
    }
    if (!event.defaultPrevented && type === "click") {
      const control = target.closest("button, summary");
      if (control?.type === "submit") submits++;
      if (control?.tagName === "SUMMARY") control.parentElement.open = !control.parentElement.open;
    }
    return event;
  }
  const controller = create({ root: doc, now: () => time,
    onSpeak: message => { spoken.push(message); order.push("speak"); },
    onActivate: () => { activations++; order.push("activate"); } });
  function pointer(type, target, data = {}) {
    return dispatch(type, target, { pointerType: "touch", pointerId: nextPointer, clientX: 50, clientY: 50, isPrimary: true, ...data });
  }
  function tap(target, { delay = 100, pointerType = "touch", detail = 1, legacy = false } = {}) {
    time += delay; const id = nextPointer++;
    pointer("pointerdown", target, { pointerId: id, pointerType });
    time += 20; pointer("pointerup", target, { pointerId: id, pointerType });
    return dispatch("click", target, legacy ? { detail } : { pointerId: id, pointerType, detail });
  }
  return { controller, doc, body, add, dispatch, pointer, tap, spoken, actions, order,
    advance: ms => { time += ms; }, activations: () => activations, submits: () => submits };
}

test("첫 터치는 중첩 요소의 버튼 이름만 읽고 두 번째 터치는 원래 이벤트로 한 번 실행한다", () => {
  const h = harness(), button = h.add("안내 시작"), child = h.add("", { tag: "span", parent: button });
  let delegated = 0; h.body.addEventListener("click", () => delegated++);
  const first = h.tap(child);
  assert.equal(first.defaultPrevented, true); assert.equal(h.actions.length, 0); assert.equal(delegated, 0);
  assert.match(h.spoken[0], /^안내 시작\./); assert.equal(button.classList.contains("touch-selected"), true);
  const second = h.tap(child);
  assert.equal(second.defaultPrevented, false); assert.equal(h.actions.length, 1); assert.equal(delegated, 1);
  assert.equal(h.actions[0].event, second); assert.equal(second.isTrusted, true);
  assert.deepEqual(h.order, ["speak", "activate", "action"]);
  assert.equal(button.classList.contains("touch-selected"), false);
});

test("숫자 키패드와 제출 버튼은 한 번 누를 때 입력이나 기본 제출을 실행하지 않는다", () => {
  const h = harness(), key = h.add("1", { dataset: { key: "1" } }), submit = h.add("버스 찾기", { type: "submit" });
  h.tap(key); assert.equal(h.actions.length, 0);
  h.tap(key); assert.equal(h.actions.length, 1);
  h.advance(400); h.tap(submit); assert.equal(h.submits(), 0);
  h.tap(submit); assert.equal(h.submits(), 1); assert.equal(h.actions.length, 2);
});

test("느린 반복과 다른 버튼 선택은 실행하지 않고 현재 기능이 바뀌면 다시 안내한다", () => {
  const h = harness(), first = h.add("설정"), second = h.add("시작");
  h.tap(first); h.tap(first, { delay: 501 }); h.tap(second); h.tap(first);
  first.dataset.action = "different-action";
  h.tap(first);
  first.textContent = "다시 듣기";
  h.tap(first);
  assert.equal(h.actions.length, 0); assert.equal(h.spoken.length, 6);
  assert.match(h.spoken.at(-1), /^다시 듣기/);
});

test("화면 전환에서 reset해도 세 번째 터치는 새 화면 버튼으로 넘어가지 않는다", () => {
  const h = harness(), next = h.add("다음 화면 실행"), first = h.add("화면 이동", { onClick: () => h.controller.reset() });
  h.tap(first); h.tap(first); const trailing = h.tap(next, { delay: 30 });
  assert.equal(trailing.defaultPrevented, true); assert.equal(h.actions.length, 1); assert.equal(h.spoken.length, 1);
  assert.equal(next.classList.contains("touch-selected"), false);
  h.tap(next, { delay: 400 }); h.tap(next);
  assert.deepEqual(h.actions.map(action => action.label), ["화면 이동", "다음 화면 실행"]);
});

test("키보드·보조기술·마우스 클릭은 즉시 실행하고 남은 안내를 정리한다", () => {
  const h = harness(), button = h.add("선택");
  h.tap(button); h.dispatch("click", button, { detail: 0 });
  h.dispatch("click", button, { detail: 1 });
  h.tap(button, { pointerType: "mouse" });
  h.tap(button, { detail: 0 });
  assert.equal(h.actions.length, 4); assert.equal(h.activations(), 4);
  assert.equal(h.spoken.length, 1); assert.equal(button.classList.contains("touch-selected"), false);
});

test("펜 입력과 PointerEvent 정보 없는 호환 클릭도 실제 터치 출처를 확인한다", () => {
  const h = harness(), button = h.add("열기");
  h.tap(button, { pointerType: "pen", legacy: true }); h.tap(button, { pointerType: "pen", legacy: true });
  assert.equal(h.actions.length, 1); assert.equal(h.spoken.length, 1);
  h.advance(500); h.pointer("pointerdown", button); h.pointer("pointerup", button);
  h.advance(1001); h.dispatch("click", button);
  assert.equal(h.actions.length, 2, "오래된 터치 기록은 이후 의미 기반 클릭을 막지 않는다");
});

test("아이폰의 mouse 타입·다른 ID 호환 클릭도 첫 터치 안내와 두 번째 실행을 구분한다", () => {
  for (const pointerType of ["mouse", ""]) {
    const h = harness(), button = h.add("길동무 시작하기");
    function safariTap(pointerId) {
      h.pointer("pointerdown", button, { pointerId });
      h.pointer("pointerup", button, { pointerId });
      return h.dispatch("click", button, { pointerId: 1, pointerType, detail: 1 });
    }
    assert.equal(safariTap(7).defaultPrevented, true);
    assert.equal(h.actions.length, 0);
    assert.match(h.spoken[0], /^길동무 시작하기/);
    h.advance(120);
    assert.equal(safariTap(8).defaultPrevented, false);
    assert.equal(h.actions.length, 1);
    assert.equal(h.activations(), 1);
  }
});

test("아이폰 호환 클릭에서도 취소된 제스처와 화면 전환 직후 추가 터치는 실행하지 않는다", () => {
  const h = harness(), next = h.add("다음 화면 실행");
  const button = h.add("화면 이동", { onClick: () => h.controller.reset() });
  function safariTap(target, pointerId, cancel = false) {
    h.advance(60);
    h.pointer("pointerdown", target, { pointerId });
    if (cancel) h.dispatch("scroll", h.body);
    h.pointer("pointerup", target, { pointerId });
    return h.dispatch("click", target, { pointerId: 1, pointerType: "mouse", detail: 1 });
  }
  assert.equal(safariTap(button, 7, true).defaultPrevented, true);
  assert.equal(h.actions.length, 0);
  safariTap(button, 8); safariTap(button, 9);
  assert.equal(h.actions.length, 1);
  assert.equal(safariTap(next, 10).defaultPrevented, true);
  assert.equal(h.actions.length, 1);
});

test("아이폰 호환 정보가 있어도 실제 마우스와 다른 대상·키보드·보조기술 클릭은 가로채지 않는다", () => {
  const h = harness(), button = h.add("터치 버튼"), other = h.add("다른 버튼");
  const rawTouch = () => {
    h.pointer("pointerdown", button, { pointerId: 7 });
    h.pointer("pointerup", button, { pointerId: 7 });
  };
  rawTouch();
  h.pointer("pointerdown", button, { pointerId: 7, pointerType: "mouse" });
  h.pointer("pointerup", button, { pointerId: 7, pointerType: "mouse" });
  assert.equal(h.dispatch("click", button, { pointerId: 7, pointerType: "mouse" }).defaultPrevented, false);
  rawTouch();
  assert.equal(h.dispatch("click", other, { pointerId: 1, pointerType: "mouse" }).defaultPrevented, false);
  rawTouch();
  assert.equal(h.dispatch("click", button, { pointerId: 1, pointerType: "mouse", detail: 0 }).defaultPrevented, false);
  assert.equal(h.actions.length, 3);
  assert.equal(h.spoken.length, 0);
});

test("아이폰 호환 클릭이 여러 손가락에서 뒤늦게 와도 취소한 기록을 각각 소비한다", () => {
  const h = harness(), button = h.add("시작");
  h.pointer("pointerdown", button, { pointerId: 7 });
  h.pointer("pointerdown", button, { pointerId: 8, isPrimary: false });
  h.pointer("pointerup", button, { pointerId: 7 });
  h.pointer("pointerup", button, { pointerId: 8, isPrimary: false });
  for (let index = 0; index < 2; index++) {
    assert.equal(h.dispatch("click", button, { pointerId: 1, pointerType: "mouse" }).defaultPrevented, true);
  }
  assert.equal(h.actions.length, 0);
  assert.equal(h.spoken.length, 0);
  h.tap(button); h.tap(button);
  assert.equal(h.actions.length, 1);
});

test("아이폰 호환 클릭은 다른 버튼의 과거 ID보다 현재 터치한 대상을 따른다", () => {
  const h = harness(), old = h.add("이전"), current = h.add("현재");
  h.pointer("pointerdown", old, { pointerId: 1 });
  h.pointer("pointercancel", old, { pointerId: 1 });
  h.advance(50);
  h.pointer("pointerdown", current, { pointerId: 7 });
  h.pointer("pointerup", current, { pointerId: 7 });
  assert.equal(h.dispatch("click", current, { pointerId: 1, pointerType: "mouse" }).defaultPrevented, true);
  assert.match(h.spoken[0], /^현재/);
  assert.equal(h.actions.length, 0);
});

test("아이폰 호환 클릭은 같은 버튼의 취소된 과거 ID보다 최신 터치를 따른다", () => {
  for (const pointerType of ["mouse", ""]) {
    const h = harness(), button = h.add("시작");
    h.pointer("pointerdown", button, { pointerId: 1 });
    h.pointer("pointercancel", button, { pointerId: 1 });
    h.advance(50);
    h.pointer("pointerdown", button, { pointerId: 7 });
    h.pointer("pointerup", button, { pointerId: 7 });
    assert.equal(h.dispatch("click", button, { pointerId: 1, pointerType }).defaultPrevented, true);
    assert.match(h.spoken[0], /^시작/);
    assert.equal(h.actions.length, 0);
    h.advance(100);
    h.pointer("pointerdown", button, { pointerId: 8 });
    h.pointer("pointerup", button, { pointerId: 8 });
    assert.equal(h.dispatch("click", button, { pointerId: 1, pointerType }).defaultPrevented, false);
    assert.equal(h.actions.length, 1);
  }
});

test("다른 버튼에서 손을 뗀 아이폰 호환 클릭은 그 버튼을 실행하지 않는다", () => {
  const h = harness(), first = h.add("처음"), release = h.add("손을 뗀 버튼");
  h.pointer("pointerdown", first, { pointerId: 7 });
  h.pointer("pointerup", release, { pointerId: 7 });
  assert.equal(h.dispatch("click", release, { pointerId: 1, pointerType: "mouse" }).defaultPrevented, true);
  assert.equal(h.actions.length, 0);
  assert.equal(h.spoken.length, 0);
});

test("레이블 연결, 설정 종류, 선택 상태와 접힌 요약 상태를 음성으로 안내한다", () => {
  const h = harness(), label = h.add("연결된 이름", { tag: "span" }); label.id = "name";
  const labelled = h.add("표시 이름", { attrs: { "aria-label": "대체 이름", "aria-labelledby": "name" } });
  const rate = h.add("1.5x", { dataset: { rate: "1.5" }, attrs: { "aria-pressed": "true" } });
  const size = h.add("더 크게", { dataset: { size: "1.2" } });
  const details = h.add("", { tag: "details" }), summary = h.add("정류장 정보", { tag: "summary", parent: details });
  h.tap(labelled); h.tap(rate); h.tap(size); h.tap(summary);
  assert.match(h.spoken[0], /^연결된 이름\./); assert.match(h.spoken[1], /음성 속도 1.5배, 선택됨/);
  assert.match(h.spoken[2], /글자 크기 더 크게/); assert.match(h.spoken[3], /정류장 정보, 접힘/);
  assert.equal(details.open, false); h.tap(summary); assert.equal(details.open, true);
});

test("드래그·스크롤·포인터 취소 이후 브라우저가 클릭을 보내도 실행하지 않는다", () => {
  for (const cancel of ["move", "scroll", "pointercancel", "reset"]) {
    const h = harness(), button = h.add("안내 시작"); h.tap(button);
    h.pointer("pointerdown", button, { pointerId: 9 });
    if (cancel === "move") h.pointer("pointermove", button, { pointerId: 9, clientX: 63 });
    if (cancel === "scroll") h.dispatch("scroll", h.body);
    if (cancel === "reset") h.controller.reset();
    h.pointer(cancel === "pointercancel" ? "pointercancel" : "pointerup", button, { pointerId: 9 });
    const click = h.dispatch("click", button, { pointerType: "touch", pointerId: 9 });
    assert.equal(click.defaultPrevented, true, cancel); assert.equal(h.actions.length, 0, cancel);
    assert.equal(button.classList.contains("touch-selected"), false, cancel);
  }
});

test("다른 버튼에서 손을 떼거나 두 손가락을 사용하면 버튼을 실행하지 않는다", () => {
  const h = harness(), first = h.add("이전"), second = h.add("다음");
  h.pointer("pointerdown", first, { pointerId: 1 }); h.pointer("pointerup", second, { pointerId: 1 });
  assert.equal(h.dispatch("click", second, { pointerType: "touch", pointerId: 1 }).defaultPrevented, true);
  h.pointer("pointerdown", first, { pointerId: 2 });
  h.pointer("pointerdown", second, { pointerId: 3, isPrimary: false });
  h.pointer("pointerup", second, { pointerId: 3 });
  assert.equal(h.dispatch("click", second, { pointerType: "touch", pointerId: 3 }).defaultPrevented, true);
  h.pointer("pointerup", first, { pointerId: 2 });
  assert.equal(h.dispatch("click", first, { pointerType: "touch", pointerId: 2 }).defaultPrevented, true);
  assert.equal(h.actions.length, 0); assert.equal(h.spoken.length, 0);
});

test("화면이 숨겨져 포인터 종료를 놓쳐도 새 주 터치로 다시 두 번 눌러 실행한다", () => {
  const h = harness(), button = h.add("시작");
  h.pointer("pointerdown", button, { pointerId: 8 });
  h.doc.hidden = true; h.dispatch("visibilitychange", h.doc);
  h.doc.hidden = false;
  h.tap(button);
  assert.equal(h.spoken.length, 1); assert.equal(h.actions.length, 0);
  const lateClick = h.dispatch("click", button, { pointerType: "touch", pointerId: 8 });
  assert.equal(lateClick.defaultPrevented, true); assert.equal(h.actions.length, 0);
  h.tap(button); h.tap(button);
  assert.equal(h.actions.length, 1);
});

test("두 손가락의 종료와 클릭 순서가 달라도 각 취소된 터치를 실행하지 않는다", () => {
  const h = harness(), button = h.add("시작");
  h.pointer("pointerdown", button, { pointerId: 1 });
  h.pointer("pointerdown", button, { pointerId: 2, isPrimary: false });
  h.pointer("pointerup", button, { pointerId: 2, isPrimary: false });
  h.pointer("pointerup", button, { pointerId: 1 });
  for (const pointerId of [2, 1]) {
    assert.equal(h.dispatch("click", button, { pointerType: "touch", pointerId }).defaultPrevented, true);
  }
  assert.equal(h.actions.length, 0); assert.equal(h.spoken.length, 0);
});

test("비활성·숨김·inert 컨트롤은 선택하지 않고 누르는 도중 비활성화돼도 실행하지 않는다", () => {
  for (const state of ["disabled", "hidden", "inert"]) {
    const h = harness(), parent = h.add("", { tag: "section" }), button = h.add("시작", { parent });
    if (state === "disabled") button.disabled = true; else parent[state] = true;
    h.tap(button); assert.equal(h.spoken.length, 0, state);
  }
  const h = harness(), button = h.add("시작");
  h.pointer("pointerdown", button, { pointerId: 7 }); button.disabled = true;
  h.pointer("pointerup", button, { pointerId: 7 });
  assert.equal(h.dispatch("click", button, { pointerType: "touch", pointerId: 7 }).defaultPrevented, true);
  assert.equal(h.actions.length, 0);
});

test("기본 입력·선택은 그대로 동작하고 바깥 터치·숨김·destroy는 선택을 해제한다", () => {
  const h = harness(), button = h.add("열기");
  for (const tag of ["input", "select", "textarea"]) {
    h.tap(button); const input = h.add("", { tag });
    assert.equal(h.tap(input).defaultPrevented, false);
    assert.equal(button.classList.contains("touch-selected"), false);
  }
  h.tap(button); h.doc.hidden = true; h.dispatch("visibilitychange", h.doc);
  assert.equal(button.classList.contains("touch-selected"), false);
  h.doc.hidden = false;
  h.tap(button); h.controller.destroy(); h.controller.destroy();
  assert.equal(button.classList.contains("touch-selected"), false);
  h.tap(button); assert.equal(h.actions.length, 1);
});
