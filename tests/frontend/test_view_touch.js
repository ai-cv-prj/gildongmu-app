const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const { normalize } = require("../../frontend/js/route-keypad.js");

function harness() {
  let clock = 100000, speechCallbacks, keypadCallbacks, speechActive = false, speechStops = 0;
  const nodes = new Map(), actions = [], submissions = [], spoken = [], focused = [], keypadOpens = [], keypadCloses = [];
  const keypadErrors = [];
  function node(id) {
    if (id.startsWith("guidance-")) return null;
    if (!nodes.has(id)) {
      const handlers = new Map(), classes = new Set(), properties = new Map();
      nodes.set(id, { id, textContent: "", value: "", hidden: false, disabled: false, type: "button", dataset: {},
        style: { setProperty(name, value) { properties.set(name, value); } }, properties,
        classList: { toggle(name, active) { if (active) classes.add(name); else classes.delete(name); }, contains: name => classes.has(name) },
        setAttribute(name, value) { this[name] = String(value); }, removeAttribute(name) { delete this[name]; },
        addEventListener(event, callback) { if (!handlers.has(event)) handlers.set(event, new Set()); handlers.get(event).add(callback); },
        removeEventListener(event, callback) { handlers.get(event)?.delete(callback); },
        fire(event, detail = {}) { handlers.get(event)?.forEach(callback => callback({ preventDefault() {}, target: this, ...detail })); },
        focus() { focused.push(id); },
        replaceChildren(fragment) { this.options = fragment.children; this.value = this.options[0]?.value || ""; },
      });
    }
    return nodes.get(id);
  }
  const panels = ["welcome", "home", "type", "input", "camera", "end"].map(name => {
    const panel = node(`panel-${name}`); panel.dataset.panel = name; return panel;
  });
  const actionButtons = [["camera-left", "settings-type"], ["stage-button", "manual-arrival"], ["walk-end", "end"],
    ["input-back", "back"], ["end-cancel", "cancel-end"], ["end-confirm", "confirm-end"], ["bus-number", "open-keypad"],
    ["home-start", "start"], ["type-home", "settings-home"], ["save-error-log", "save-error-log"],
    ["save-local-clip", "save-local-clip"]].map(([id, action]) => {
    const button = node(id); button.dataset.action = action; return button;
  });
  const mic = node("mic-button"); mic.dataset.mic = "input";
  const submit = node("route-submit"); submit.type = "submit";
  const rates = [1, 1.5, 2].map(value => { const button = node(`rate-${value}`); button.dataset.rate = String(value); return button; });
  const sizes = [1, 1.2, 1.5].map(value => { const button = node(`size-${value}`); button.dataset.size = String(value); return button; });
  const document = node("document");
  document.getElementById = node;
  document.createDocumentFragment = () => ({ children: [], appendChild(child) { this.children.push(child); } });
  document.createElement = () => ({});
  document.querySelectorAll = selector => ({
    "[data-panel]": panels, "[data-action]": actionButtons, "[data-mic]": [mic], "[data-rate]": rates,
    "[data-size]": sizes, "[data-route]": [node("route-caption")],
    "[data-action], [data-mic], [data-rate], [data-size], #route-form button": [...actionButtons, mic, ...rates, ...sizes, submit],
  }[selector] || []);
  document.querySelector = selector => {
    if (selector === '[data-panel="camera"]') return node("panel-camera");
    const match = selector.match(/^\[data-panel="(\w+)"\] \.top-controls button:not\(:disabled\)$/);
    return match ? node(`${match[1]}-first-button`) : null;
  };
  const context = { Date: class extends Date { static now() { return clock; } }, document, window: {
    GildongmuFilm: { mount: () => ({ setVisible() {}, restart() {}, destroy() {} }) },
    GRouteKeypad: { normalize, create(callbacks) {
      keypadCallbacks = callbacks;
      return { setValue() {}, setError(message) { keypadErrors.push(message); }, open(value) { keypadOpens.push(value); callbacks.onOpenChange(true); },
        close(options) { keypadCloses.push(options); }, destroy() {} };
    } },
    GSpeechInput: { create(callbacks) {
      speechCallbacks = callbacks;
      return { supported: true, isActive: () => speechActive,
        start(mode) { speechActive = true; callbacks.onState(true, mode); return true; },
        stop() { speechStops++; speechActive = false; callbacks.onState(false); }, destroy() {} };
    } },
  } };
  vm.runInNewContext(fs.readFileSync("frontend/js/view.js", "utf8"), context);
  const view = context.window.GView.create({ onAction: action => actions.push(action), onSubmitRoute: route => submissions.push(route),
    onSpeak: message => spoken.push(message) });
  return { view, node, actions, submissions, spoken, focused, keypadOpens, keypadCloses, keypadErrors, speechCallbacks, keypadCallbacks,
    speechStops: () => speechStops, click(id) { clock += 300; node(id).fire("click"); },
    submit() { clock += 300; node("route-form").fire("submit"); }, now: () => clock,
  };
}

test("상단 두 버튼은 이동·버스 인식 화면에서 보이는 기능을 그대로 실행한다", () => {
  const h = harness(); h.view.show("walk");
  assert.equal(h.node("camera-left-label").textContent, "글자 크기 설정");
  assert.equal(h.node("stage-label").textContent, "버스 번호 입력");
  h.click("camera-left"); h.click("stage-button");
  assert.deepEqual(h.actions, ["settings-type", "manual-arrival"]);
  h.view.setRoute("강남02"); h.view.show("search");
  assert.equal(h.node("camera-route").textContent, "강남02번");
  assert.equal(h.node("camera-left-label").textContent, "이전 화면으로");
  assert.equal(h.node("stage-label").textContent, "다시 듣기");
  h.click("camera-left"); h.click("stage-button");
  assert.deepEqual(h.actions.slice(-2), ["back", "repeat"]);
  h.view.show("type"); assert.equal(h.node("panel-type").hidden, false);
  h.view.show("end"); assert.equal(h.node("panel-end").hidden, false);
  h.click("end-cancel"); h.click("end-confirm");
  assert.deepEqual(h.actions.slice(-2), ["cancel-end", "confirm-end"]);
  h.view.destroy();
});

test("OCR·도착정보와 카메라 상태 변경은 사용자 초점을 빼앗지 않는다", () => {
  const h = harness(); h.view.show("walk");
  const focusCount = h.focused.length;
  h.view.show("search"); h.view.setBus({ route: "143", ocrStatus: "confirmed", ocrCapturedAt: h.now(), ocrMessage: "143번 버스가 도착했습니다." });
  h.view.show("approach"); h.view.show("arrived"); h.view.render(null);
  assert.match(h.view.getGuidance(), /^143번 버스가 도착했습니다/);
  h.view.setBus({ ocrStatus: "other", ocrCapturedAt: h.now(), ocrMessage: "604번 버스, 다른 노선." });
  assert.match(h.view.getGuidance(), /^604번 버스, 다른 노선/);
  assert.equal(h.focused.length, focusCount);
  h.view.show("input");
  assert.equal(h.focused.at(-1), "input-first-button");
  assert.equal(h.keypadCloses.at(-1).restoreFocus, false);
  h.view.destroy();
});

test("최종 음성 번호는 확인 화면 없이 제출하고 잘못된 문장과 화면을 벗어난 결과는 무시한다", () => {
  const h = harness(); h.view.show("input");
  h.click("mic-button");
  assert.equal(h.node("mic-label").textContent, "듣는 중");
  assert.equal(h.actions.at(-1), "speech-start");
  h.speechCallbacks.onResult({ mode: "input", value: "엔 이십육 번" });
  assert.deepEqual(h.submissions, ["N26"]);
  assert.equal(h.view.readRoute(), "N26");
  assert.equal(h.node("mic-label").textContent, "음성 입력");
  assert.equal(h.actions.at(-1), "speech-end");
  h.speechCallbacks.onResult({ mode: "input", value: "143 아니고 271" });
  assert.equal(h.submissions.length, 1);
  assert.equal(h.node("route-error").textContent, "번호 다시 입력");
  h.view.show("walk"); h.speechCallbacks.onResult({ mode: "input", value: "7016" });
  assert.equal(h.submissions.length, 1);
  h.view.destroy();
});

test("번호 버튼과 폼은 현재 값을 공유하고 편집 시 기존 오류를 지운다", () => {
  const h = harness(); h.view.show("input"); h.view.setRoute("112-1");
  h.click("bus-number");
  assert.deepEqual(h.keypadOpens, ["112-1"]);
  assert.ok(h.speechStops() > 0);
  h.submit(); assert.deepEqual(h.submissions, ["112-1"]);
  h.view.setRoute("12345"); h.submit();
  assert.equal(h.submissions.length, 1);
  assert.equal(h.node("route-error").hidden, false);
  h.keypadCallbacks.onChange("강남02");
  assert.equal(h.view.readRoute(), "강남02");
  assert.equal(h.node("route-error").hidden, true);
  h.keypadCallbacks.onSubmit("강남02");
  assert.deepEqual(h.submissions, ["112-1", "강남02"]);
  h.view.destroy();
});

test("음성 인식 실패 시 마이크를 종료하고 오류 안내를 재생한다", () => {
  const h = harness(); h.view.show("input"); h.click("mic-button");
  h.speechCallbacks.onError("마이크 권한이 필요합니다.");
  assert.equal(h.actions.at(-1), "speech-end");
  assert.equal(h.node("mic-label").textContent, "음성 입력");
  assert.equal(h.node("route-error").textContent, "마이크 권한이 필요합니다.");
  assert.equal(h.spoken.at(-1), "마이크 권한이 필요합니다.");
  assert.deepEqual(h.submissions, []);
  h.view.destroy();
});

test("입력 준비가 끝나면 열린 키패드의 대기 오류만 지우고 입력값을 보존한다", () => {
  const h = harness(); h.view.show("input"); h.view.setRoute("143"); h.click("bus-number");
  const closes = h.keypadCloses.length;
  h.view.setRouteError("잠시 후 버스를 선택해 주세요.");
  assert.match(h.keypadErrors.at(-1), /잠시 후/);
  h.view.setRouteError("");
  assert.equal(h.keypadErrors.at(-1), "");
  assert.equal(h.view.readRoute(), "143");
  assert.equal(h.keypadCloses.length, closes);
  h.view.destroy();
});

test("처리·일시중지 중에는 입력을 막으면서 종료와 뒤로가기는 유지한다", () => {
  const h = harness(); h.view.show("input"); h.view.setRoute("143"); h.view.setBusy(true);
  h.click("bus-number"); h.click("mic-button"); h.submit(); h.keypadCallbacks.onSubmit("143");
  h.speechCallbacks.onResult({ mode: "input", value: "143" });
  assert.deepEqual(h.submissions, []); assert.deepEqual(h.keypadOpens, []);
  h.click("input-back"); h.click("walk-end");
  assert.deepEqual(h.actions, ["back", "end"]);
  h.view.setBusy(false); h.view.setPaused(true);
  h.click("bus-number"); h.click("mic-button"); h.submit();
  assert.deepEqual(h.submissions, []); assert.deepEqual(h.keypadOpens, []);
  h.view.setPaused(false); h.submit();
  assert.deepEqual(h.submissions, ["143"]);
  h.view.destroy();
});

test("설정 로딩 실패나 전송 처리 중에도 오류 로그 저장을 사용할 수 있다", () => {
  const h = harness(); h.view.setBusy(true); h.view.setPaused(true);
  assert.equal(h.node("save-error-log").disabled, false);
  h.click("save-error-log");
  assert.deepEqual(h.actions, ["save-error-log"]);
  h.view.destroy();
});

test("글자 크기 설정은 선택 표시와 화면 배율을 함께 복원한다", () => {
  const h = harness();
  h.view.setSettings({ textScale: 1.5, rate: 2 });
  assert.equal(h.node("app").properties.get("--text-scale"), "1.5");
  assert.equal(h.node("size-1.5")["aria-pressed"], "true");
  assert.equal(h.node("size-1")["aria-pressed"], "false");
  assert.equal(h.node("rate-2")["aria-pressed"], "true");
  h.view.setSettings({ textScale: 2 });
  assert.equal(h.node("app").properties.get("--text-scale"), "1.5", "이전 최대 크기 설정을 새 최대값으로 옮긴다");
  h.view.destroy();
});

test("보관 영상 선택과 저장은 처리 중에도 가능하고 저장 실패를 구분해 표시한다", () => {
  const h = harness(), records = [1, 2].map(index => ({ key: `session:${index}`, durable: true,
    clip: { index, started_at_ms: 1700000000000 + index * 1000 } }));
  h.view.setLocalClips(records);
  assert.equal(h.node("local-clips-panel").hidden, false);
  assert.equal(h.view.readLocalClipKey(), "session:1");
  h.node("local-clip-choice").value = "session:2";
  h.view.setLocalClips(records);
  assert.equal(h.view.readLocalClipKey(), "session:2");
  h.view.setBusy(true); h.click("save-local-clip");
  assert.equal(h.actions.at(-1), "save-local-clip");
  h.view.setLocalClips([{ ...records[0], durable: false }]);
  assert.match(h.node("local-clips-status").textContent, /보관 실패/);
  h.view.setLocalClips([]);
  assert.equal(h.node("local-clips-panel").hidden, true);
  h.view.destroy();
});
