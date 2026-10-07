const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const { normalize, composeHangul } = require("../../frontend/js/route-keypad.js");

test("노선 번호와 음성으로 말한 한국어·영문 접두사를 정규화한다", () => {
  for (const [raw, expected] of [
    ["143", "143"], [" N26 ", "N26"], ["강남02", "강남02"], ["7016", "7016"], ["112-1", "112-1"],
    ["１１２－１", "112-1"], ["1 4 3", "143"], ["버스 번호는 143번입니다.", "143"],
    ["일사삼", "143"], ["백사십삼", "143"], ["칠천십육 번", "7016"], ["강남 공이", "강남02"],
    ["엔 이십육 번 버스", "N26"], ["m 6 4 1 0", "M6410"], ["일반 버스 143번 타고 싶어요", "143"],
  ]) assert.equal(normalize(raw), expected, raw);
});

test("대화·불완전한 번호·네 자리를 넘는 숫자는 노선으로 제출하지 않는다", () => {
  for (const raw of [null, 143, "", "버스 찾아 주세요", "아니 143", "143 아니고 271", "버스 아무거나", "143 또는 271",
    "번호 다시 입력", "12345", "7016-1", "강남12345", "ABCDE123", "143-", "-143", "14--3", "십십", "ㄱㄴ02", "x".repeat(161)]) {
    assert.equal(normalize(raw), null, String(raw));
  }
});

test("한글 키패드 자모를 노선 접두사로 조합하며 받침과 복합 모음을 구분한다", () => {
  for (const [raw, expected] of [
    ["ㄱㅏㅇㄴㅏㅁ", "강남"], ["ㅅㅓㅊㅗ", "서초"], ["ㄱㅗㅏㄴㅇㅏㄱ", "관악"],
    ["ㄱㅏㄴㅏ", "가나"], ["ㄷㅏㄹㄱ", "닭"], ["ㄷㅏㄹㄱㅏ", "달가"],
    ["ㅂㅜㅓㄴ", "붠"], ["강남ㄱㅏ", "강남가"], ["Nㄱ", "Nㄱ"], ["", ""],
  ]) assert.equal(composeHangul(raw), expected, raw);
});

function harness() {
  let doc;
  class Element {
    constructor(tag = "div", id = "") {
      this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.parentElement = null;
      this.dataset = {}; this.hidden = false; this.inert = false; this.disabled = false; this.textContent = "";
      this.attributes = {}; this.listeners = new Map(); this.className = "";
      this.classList = { toggle: (name, enabled) => {
        const classes = new Set(this.className.split(" ").filter(Boolean));
        if (enabled) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(" ");
      } };
    }
    append(...children) { children.forEach(child => { this.children.push(child); child.parentElement = this; }); }
    replaceChildren(...children) { this.children.forEach(child => { child.parentElement = null; }); this.children = []; this.append(...children); }
    contains(other) { return this === other || this.children.some(child => child.contains(other)); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(event, callback) { if (!this.listeners.has(event)) this.listeners.set(event, new Set()); this.listeners.get(event).add(callback); }
    removeEventListener(event, callback) { this.listeners.get(event)?.delete(callback); }
    fire(event, value) { this.listeners.get(event)?.forEach(callback => callback(value)); }
    focus() { doc.activeElement = this; }
    matches(selector) {
      if (selector === "[hidden]") return this.hidden;
      if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
      const attr = selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);
      if (attr) {
        const name = attr[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
        return attr[2] === undefined ? this.dataset[name] !== undefined : this.dataset[name] === attr[2];
      }
      return this.tagName.toLowerCase() === selector;
    }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }
  const nodes = new Map();
  function make(id, parent, tag = "div", data = {}) {
    const node = new Element(tag, id); node.dataset = data;
    if (id) nodes.set(id, node);
    if (parent) parent.append(node);
    return node;
  }
  doc = new Element("document"); doc.createElement = tag => new Element(tag); doc.getElementById = id => nodes.get(id);
  const app = make("app", doc), panel = make("panel", app), other = make("other", app);
  other.inert = true;
  const trigger = make("bus-number", panel, "button"); make("", trigger, "span");
  const layer = make("route-keypad-layer", app); layer.hidden = true;
  const dialog = make("route-keypad-dialog", layer);
  const close = make("close", dialog, "button", { keypadAction: "close" });
  make("search", dialog, "button", { keypadAction: "search" });
  const output = make("route-keypad-output", dialog, "output"); make("", output, "span");
  make("route-keypad-error", dialog, "p");
  const modes = make("", dialog); modes.className = "letter-modes";
  for (const name of ["consonants", "vowels", "english"]) make("", modes, "button", { letterGroup: name });
  const numbers = make("number-keys", dialog);
  for (const value of [..."123456789", "letters", "0", "delete"]) make("", numbers, "button", { key: value });
  make("letter-keys", dialog);
  const changes = [], submissions = [], speech = [], transitions = [];
  const context = { window: { document: doc } };
  vm.runInNewContext(fs.readFileSync("frontend/js/route-keypad.js", "utf8"), context);
  const keypad = context.window.GRouteKeypad.create({
    onChange: value => changes.push(value), onSubmit: value => submissions.push(value),
    onSpeak: value => speech.push(value), onOpenChange: value => transitions.push(value),
  });
  return { keypad, changes, submissions, speech, transitions, doc, panel, other, trigger, close, nodes,
    click(selector) {
      const button = layer.querySelector(selector); assert.ok(button, selector);
      button.focus(); layer.fire("click", { target: button });
    },
    press(key, shiftKey = false) {
      const event = { key, shiftKey, prevented: false, preventDefault() { this.prevented = true; } };
      doc.fire("keydown", event); return event;
    },
  };
}

test("키패드는 배경을 잠그고 닫을 때 기존 inert 상태와 번호 버튼 초점을 복원한다", () => {
  const h = harness();
  h.keypad.open("143");
  assert.equal(h.keypad.isOpen(), true);
  assert.equal(h.panel.inert, true);
  assert.equal(h.other.inert, true);
  assert.equal(h.doc.activeElement, h.close);
  assert.equal(h.trigger.value, "143");
  assert.deepEqual(h.changes, []);
  h.keypad.open("N26");
  h.press("Escape");
  assert.equal(h.keypad.isOpen(), false);
  assert.equal(h.panel.inert, false);
  assert.equal(h.other.inert, true);
  assert.equal(h.doc.activeElement, h.trigger);
  h.keypad.destroy();
});

test("키보드·터치 입력은 숫자 네 자리 제한과 삭제·하이픈·제출을 공유한다", () => {
  const h = harness(); h.keypad.open();
  for (const digit of "112") h.click(`[data-key="${digit}"]`);
  h.press("-"); h.press("1"); h.press("8");
  assert.equal(h.trigger.value, "112-1");
  assert.equal(h.speech.at(-1), "숫자 네 자리까지");
  h.nodes.get("route-keypad-output").focus();
  h.press("Enter");
  assert.deepEqual(h.submissions, ["112-1"]);
  h.press("Backspace");
  h.press("Enter");
  assert.equal(h.submissions.length, 1);
  assert.equal(h.nodes.get("route-keypad-error").hidden, false);
  h.press("1");
  assert.equal(h.nodes.get("route-keypad-error").hidden, true);
  h.keypad.destroy();
});

test("버튼에 초점을 둔 Enter는 닫기·숫자 버튼의 기본 동작을 유지한다", () => {
  const h = harness(); h.keypad.open("143");
  assert.equal(h.doc.activeElement, h.close);
  assert.equal(h.press("Enter").prevented, false);
  assert.deepEqual(h.submissions, []);
  h.click('[data-key="1"]');
  assert.equal(h.press("Enter").prevented, false);
  assert.deepEqual(h.submissions, []);
  h.keypad.destroy();
});

test("자음·모음 페이지 입력으로 강남02를 만들고 문자 삭제는 숫자를 보존한다", () => {
  const h = harness(); h.keypad.open("02", "letters");
  const group = name => h.click(`[data-letter-group="${name}"]`);
  const letter = char => h.click(`[data-letter="${char}"]`);
  letter("ㄱ"); group("vowels"); letter("ㅏ"); group("consonants");
  h.click('[data-letter-action="page"]'); letter("ㅇ");
  group("consonants"); letter("ㄴ"); group("vowels"); letter("ㅏ"); group("consonants"); letter("ㅁ");
  assert.equal(h.trigger.value, "강남02");
  assert.equal(h.doc.activeElement.dataset.letter, "ㅁ");
  h.click('[data-keypad-action="search"]');
  assert.deepEqual(h.submissions, ["강남02"]);
  h.click('[data-letter-action="delete"]');
  assert.equal(h.trigger.value, "강나02", "조합 중에는 마지막 자모만 지운다");
  h.keypad.setValue("강남02");
  h.click('[data-letter-action="delete"]');
  assert.equal(h.trigger.value, "강02", "외부에서 설정한 완성된 음절은 문자 단위로 지운다");
  h.keypad.destroy();
});

test("영문 다음 페이지와 숫자 전환을 사용하고 Tab 초점은 팝업 안에서 순환한다", () => {
  const h = harness(); h.keypad.open("", "letters");
  h.click('[data-letter-group="english"]');
  h.click('[data-letter-action="page"]'); h.click('[data-letter="N"]');
  h.click('[data-letter-action="numbers"]'); h.press("2"); h.press("6");
  assert.equal(h.trigger.value, "N26");
  h.close.focus();
  assert.equal(h.press("Tab", true).prevented, true);
  assert.equal(h.doc.activeElement.dataset.key, "delete");
  assert.equal(h.press("Tab").prevented, true);
  assert.equal(h.doc.activeElement, h.close);
  h.keypad.close({ restoreFocus: false });
  assert.equal(h.doc.activeElement, h.close);
  h.keypad.destroy();
  h.keypad.open("143");
  assert.equal(h.keypad.isOpen(), false);
  h.press("9");
  assert.equal(h.trigger.value, "N26");
});
