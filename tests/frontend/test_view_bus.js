const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

function harness() {
  let clock = 100000;
  const nodes = new Map();
  function node(id) {
    if (id.startsWith("guidance-")) return null;
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
    ocrStatus: "confirmed", ocrCapturedAt: clock, ocrMessage: "143번 버스 확인함." });
  return { view, node, now: () => clock, step(ms) { clock += ms; } };
}

test("결과 카드 없이도 확인한 목표 버스 번호의 음성 안내를 제공한다", () => {
  const { view, node } = harness();
  assert.equal(node("camera-title-text").textContent, " 버스 인식 중");
  assert.equal(node("camera-route").textContent, "143번");
  assert.equal(view.getGuidance(), "143번 버스 확인함. 버스 번호와 주변 상황을 함께 확인해 주세요.");
  assert.equal(node("bus-arrival-text").textContent, "도착정보 이용 불가");
  assert.equal(view.getScreen(), "search", "번호 확인만으로 정류장 도착 상태로 바꾸지 않는다");
  view.destroy();
});

test("다른 버스의 번호는 음성으로 안내하며 목표 번호를 유지한다", () => {
  const { view, node, now } = harness();
  view.setBus({ ocrStatus: "other", ocrCapturedAt: now(),
    ocrMessage: "604번 버스, 다른 노선." });
  assert.equal(view.getGuidance(), "604번 버스, 다른 노선. 찾는 버스의 번호를 계속 확인합니다.");
  assert.equal(node("camera-route").textContent, "143번");
  assert.equal(view.getScreen(), "search");
  view.destroy();
});

test("번호 확인 중 긴급 경고가 우선하며 경고 해제 후 번호 확인 안내로 돌아간다", () => {
  const { view, now } = harness();
  view.render({ captured_at_ms: now(), walking: { event: { level: "danger", voice_action: "stop", voice_text: "멈추세요." } } });
  assert.equal(view.getGuidance(), "멈추세요. 잠시 멈추고 주변을 확인해 주세요.");
  assert.equal(view.getScreen(), "search");
  view.render({ captured_at_ms: now(), walking: { event: { level: "safe" } } });
  assert.match(view.getGuidance(), /^143번 버스 확인함/);
  view.destroy();
});

test("지난 번호 확인 결과와 다른 목표 노선의 결과는 다시 읽지 않는다", () => {
  const { view, now, step } = harness();
  step(3001);
  assert.equal(view.getGuidance(), "버스를 찾고 있어요. 버스가 오는 쪽을 바라보세요.");
  view.setBus({ ocrCapturedAt: now() });
  assert.match(view.getGuidance(), /^143번 버스 확인함/);
  view.setRoute("271");
  assert.equal(view.getGuidance(), "버스를 찾고 있어요. 버스가 오는 쪽을 바라보세요.");
  view.destroy();
});

test("카메라 모델 준비·오류는 도착정보 오류와 구분하여 음성 안내한다", () => {
  const { view, node } = harness();
  view.setBus({ ocrStatus: "loading", ocrCapturedAt: null, ocrMessage: "143번 버스 번호 인식을 준비하고 있습니다." });
  assert.equal(view.getGuidance(), "번호 인식을 준비하고 있어요. 143번 버스 번호 인식을 준비하고 있습니다.");
  view.setBus({ ocrStatus: "error", ocrMessage: "카메라 번호 인식에 필요한 모델 파일이 없습니다." });
  assert.equal(view.getGuidance(), "번호 인식을 사용할 수 없어요. 카메라 번호 인식에 필요한 모델 파일이 없습니다.");
  assert.equal(node("bus-arrival-text").textContent, "도착정보 이용 불가");
  view.destroy();
});

test("정류장에서는 이전 장애물 경고를 읽지 않고 보행 복귀 후 다시 안내한다", () => {
  const { view, now } = harness();
  view.render({ captured_at_ms: now(), walking: { event: { level: "danger", voice_action: "stop", voice_text: "멈추세요." } } });
  view.setObstacleDetection(false);
  assert.match(view.getGuidance(), /^143번 버스 확인함/);
  view.setObstacleDetection(true);
  assert.match(view.getGuidance(), /^멈추세요/);
  view.destroy();
});

test("다시 듣기는 만료된 긴급 경고 대신 현재 버스 안내를 반환한다", () => {
  const { view, now, step } = harness();
  view.render({ captured_at_ms: now(), walking: { event: { level: "danger", voice_action: "stop", voice_text: "멈추세요." } } });
  assert.match(view.getGuidance(), /^멈추세요/);
  step(1500);
  assert.match(view.getGuidance(), /^143번 버스 확인함/);
  view.destroy();
});

test("일시중지 중에는 멈춤 안내를 제공하고 재개 시 최신 번호 안내를 복원한다", () => {
  const { view, node, now } = harness();
  view.setPaused(true);
  view.setBus({ ocrStatus: "other", ocrCapturedAt: now(), ocrMessage: "604번 버스, 다른 노선." });
  assert.equal(view.getGuidance(), "안내를 잠시 멈췄어요. 촬영과 자동 안내가 중지됩니다.");
  assert.equal(node("pause-card").hidden, false);
  view.setPaused(false);
  assert.equal(node("pause-card").hidden, true);
  assert.equal(view.getGuidance(), "604번 버스, 다른 노선. 찾는 버스의 번호를 계속 확인합니다.");
  view.destroy();
});
