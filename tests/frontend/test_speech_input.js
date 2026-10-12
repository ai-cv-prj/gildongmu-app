const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { normalize } = require("../../frontend/js/route-keypad.js");

function harness() {
  const recognitions = [], states = [], results = [], errors = [];
  class Recognition {
    constructor() { this.aborts = 0; this.starts = 0; recognitions.push(this); }
    start() { this.starts++; }
    abort() { this.aborts++; }
  }
  const context = { window: { SpeechRecognition: Recognition, GRouteKeypad: { normalize } } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("frontend/js/speech-input.js", "utf8"), context);
  const speech = context.window.GSpeechInput.create({
    onState: (active, mode, suspended) => states.push({ active, mode, suspended }),
    onResult: result => results.push({ ...result }),
    onError: error => errors.push(error),
  });
  return { speech, recognitions, states, results, errors };
}

function resultEvent(transcript, isFinal = true) {
  const result = [{ transcript }]; result.isFinal = isFinal;
  return { resultIndex: 0, results: [result] };
}

test("음성 입력은 확정된 유효 노선 번호를 한 번 전달한다", () => {
  const h = harness();
  assert.equal(h.speech.start("input"), true);
  const recognition = h.recognitions[0];
  recognition.onstart();
  recognition.onresult(resultEvent("버스 번호는 143번입니다.", false));
  assert.equal(h.results.length, 0);
  recognition.onresult(resultEvent("버스 번호는 143번입니다."));
  recognition.onresult(resultEvent("271"));
  assert.deepEqual(h.results, [{ mode: "input", value: "143", transcript: "버스 번호는 143번입니다." }]);
  recognition.onend();
  assert.equal(h.speech.isActive(), false);
  assert.deepEqual(h.states.at(-1), { active: false, mode: "input", suspended: false });
  assert.deepEqual(h.errors, []);
});

test("버튼 안내 중 인식을 중단해도 취소 가능한 세션과 모드는 유지한다", () => {
  const h = harness();
  assert.equal(h.speech.suspend(), false);
  h.speech.start("input");
  const recognition = h.recognitions[0];
  assert.equal(h.speech.suspend(), true);
  assert.equal(h.speech.isActive(), true);
  assert.equal(recognition.aborts, 1);
  for (const callback of ["onstart", "onresult", "onerror", "onend"]) assert.equal(recognition[callback], null);
  assert.deepEqual(h.states.at(-1), { active: true, mode: "input", suspended: true });
  const stateCount = h.states.length;
  assert.equal(h.speech.suspend(), false);
  assert.equal(recognition.aborts, 1);
  assert.equal(h.states.length, stateCount);
  assert.equal(h.recognitions.length, 1, "마이크를 자동으로 다시 열지 않는다");
});

test("일시중지 전 예약된 인식 결과·오류·종료는 노선이나 상태를 바꾸지 않는다", () => {
  const h = harness(); h.speech.start("input");
  const { onstart, onresult, onerror, onend } = h.recognitions[0];
  h.speech.suspend();
  const stateCount = h.states.length;
  onstart();
  onresult(resultEvent("7016"));
  onerror({ error: "network" });
  onend();
  assert.deepEqual(h.results, []);
  assert.deepEqual(h.errors, []);
  assert.equal(h.states.length, stateCount);
  assert.equal(h.speech.isActive(), true);
});

test("일시중지된 음성 입력을 취소한 뒤 명시적으로 새 인식을 시작한다", () => {
  const h = harness(); h.speech.start("input");
  const oldResult = h.recognitions[0].onresult;
  h.speech.suspend(); h.speech.stop();
  assert.equal(h.speech.isActive(), false);
  assert.deepEqual(h.states.at(-1), { active: false, mode: "input", suspended: false });
  assert.equal(h.speech.suspend(), false);
  assert.equal(h.speech.start("input"), true);
  assert.equal(h.recognitions.length, 2);
  oldResult(resultEvent("143"));
  h.recognitions[1].onstart();
  h.recognitions[1].onresult(resultEvent("N26"));
  assert.deepEqual(h.results, [{ mode: "input", value: "N26", transcript: "N26" }]);
  assert.deepEqual(h.states.at(-1), { active: true, mode: "input", suspended: false });
  h.speech.destroy();
  assert.equal(h.speech.isActive(), false);
  assert.equal(h.recognitions[1].aborts, 1);
});

test("확인 모드도 일시중지 중 유지하고 새 시작은 인식 모드를 바꿀 수 있다", () => {
  const h = harness(); h.speech.start("confirm"); h.speech.suspend();
  assert.deepEqual(h.states.at(-1), { active: true, mode: "confirm", suspended: true });
  h.speech.start("input");
  assert.deepEqual(h.states.at(-1), { active: true, mode: "input", suspended: false });
  h.recognitions[1].onresult(resultEvent("강남 공이"));
  assert.equal(h.results[0].value, "강남02");
  assert.deepEqual(h.errors, []);
});
