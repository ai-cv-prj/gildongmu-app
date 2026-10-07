const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness() {
  let clock = 100000;
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, { textContent: "", value: "", hidden: false, disabled: false,
      dataset: {}, style: { setProperty() {} }, classList: { toggle() {} },
      setAttribute(name, value) { this[name] = value; }, addEventListener() {}, removeEventListener() {} });
    return nodes.get(id);
  }
  const panels = ["welcome", "home", "type", "input", "camera", "end"].map(name => {
    const panel = node(`panel-${name}`); panel.dataset.panel = name; return panel;
  });
  const context = { Date: { now: () => clock },
    window: {
      GildongmuFilm: { mount: () => ({ setVisible() {}, restart() {}, destroy() {} }) },
      GSpeechInput: { create: () => ({ stop() {}, destroy() {} }) },
      GRouteKeypad: { normalize: require("../../frontend/js/route-keypad.js").normalize,
        create: () => ({ setValue() {}, setError() {}, close() {}, destroy() {} }) },
    }, document: { getElementById: node,
      addEventListener() {}, removeEventListener() {},
      querySelectorAll: selector => selector === "[data-panel]" ? panels : [],
      querySelector: selector => selector === '[data-panel="camera"]' ? node("panel-camera") : null } };
  vm.runInNewContext(fs.readFileSync("frontend/js/view.js", "utf8"), context);
  const view = context.window.GView.create();
  view.show("search");
  view.setBus({ route: "143", station: "정류장 정보 없음", arrivalText: "도착정보 이용 불가",
    gpsMessage: "도착정보를 사용할 수 없습니다. 카메라 번호 인식은 계속됩니다.",
    ocrStatus: "confirmed", ocrCapturedAt: clock, ocrMessage: "143번 버스가 도착했습니다." });
  return { view, node, now: () => clock, step(ms) { clock += ms; } };
}

test("도착정보 없이 확인한 목표 버스 번호를 주 안내 카드에 표시한다", () => {
  const { view, node } = harness();
  assert.equal(node("camera-title-text").textContent, " 버스 인식 중");
  assert.equal(node("camera-route").textContent, "143번");
  assert.equal(node("guidance-label").textContent, "카메라 번호 인식");
  assert.match(node("guidance-title").textContent, /143번 버스가 도착했습니다/);
  assert.equal(node("bus-arrival-text").textContent, "도착정보 이용 불가");
  assert.equal(view.getScreen(), "search", "번호 확인만으로 정류장 도착 상태로 바꾸지 않는다");
  view.destroy();
});

test("번호 확인 중 긴급 경고가 우선하며 경고 해제 후 번호 확인 안내로 돌아간다", () => {
  const { view, node, now } = harness();
  view.render({ captured_at_ms: now(), walking: { event: { level: "danger", voice_action: "stop", voice_text: "멈추세요." } } });
  assert.equal(node("guidance-title").textContent, "멈추세요.");
  assert.equal(view.getScreen(), "search");
  view.render({ captured_at_ms: now(), walking: { event: { level: "safe" } } });
  assert.match(node("guidance-title").textContent, /143번 버스가 도착했습니다/);
  view.destroy();
});

test("지난 번호 확인 결과와 다른 목표 노선의 결과는 안내 카드에 남기지 않는다", () => {
  const { view, node, step } = harness();
  step(3001);
  view.render(null);
  assert.equal(node("guidance-label").textContent, "목표 버스 탐색");
  view.setRoute("271");
  view.show("search");
  assert.doesNotMatch(node("guidance-title").textContent, /143/);
  view.destroy();
});

test("카메라 모델 준비·오류는 도착정보 오류와 다른 상태로 표시한다", () => {
  const { view, node } = harness();
  view.setBus({ ocrStatus: "loading", ocrCapturedAt: null, ocrMessage: "143번 버스 번호 인식을 준비하고 있습니다." });
  assert.match(node("guidance-title").textContent, /준비/);
  view.setBus({ ocrStatus: "error", ocrMessage: "카메라 번호 인식에 필요한 모델 파일이 없습니다." });
  assert.match(node("guidance-title").textContent, /번호 인식을 사용할 수 없어요/);
  assert.match(node("guidance-copy").textContent, /모델 파일/);
  assert.equal(node("bus-arrival-text").textContent, "도착정보 이용 불가");
  view.destroy();
});

test("정류장에서는 이전 장애물 경고를 숨기고 보행 복귀 후 다시 표시한다", () => {
  const { view, node, now } = harness();
  view.render({ captured_at_ms: now(), walking: { event: { level: "danger", voice_action: "stop", voice_text: "멈추세요." } } });
  view.setObstacleDetection(false);
  assert.equal(node("guidance-title").textContent, "143번 버스가 도착했습니다.");
  view.setObstacleDetection(true);
  assert.equal(node("guidance-title").textContent, "멈추세요.");
  view.destroy();
});
