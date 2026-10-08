/** Offline bus review interactions, with browser media and DOM boundaries stubbed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const html = fs.readFileSync("result_hub/static/bus_review.html", "utf8");
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.currentTime = 0;
    this.duration = 30;
    this.readyState = 1;
    this.paused = true;
    this.playbackRate = 1;
    this.classList = {
      add: name => { this.className = [...new Set([...this.className.split(/\s+/), name])].join(" ").trim(); },
      remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(" "); },
    };
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map(child => child.textContent).join(""); }
  set innerHTML(value) { throw new Error("Review data must use textContent, never HTML parsing"); }
  get childNodes() { return this.children; }
  set src(value) { this.setAttribute("src", value); }
  get src() { return this.getAttribute("src"); }
  set href(value) { this.setAttribute("href", value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ""; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  querySelectorAll(selector) {
    const [tag, className] = selector.split(".");
    return this.children.flatMap(child => [
      ...(child.tagName === tag.toUpperCase() && (!className || child.className.split(/\s+/).includes(className)) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, callback, options) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), { callback, once: options?.once }]);
  }
  async emit(type) {
    const listeners = [...(this.listeners.get(type) || [])];
    this.listeners.set(type, listeners.filter(listener => !listener.once));
    await Promise.all(listeners.map(listener => listener.callback({ target: this })));
  }
  async click() { if (!this.disabled) await this.emit("click"); }
  pause() {
    const playing = !this.paused;
    this.paused = true;
    if (playing) void this.emit("pause");
  }
  async play() { this.paused = false; }
  load() { this.currentTime = 0; this.paused = true; }
}

function sample(values = {}) {
  return { frame_id: 8, bus_frame_id: 4, source_offset_ms: 1200, offset_ms: 2200,
    inference_video_ms: 1600, target_route: "7011", status: "matched", bus_count: 1,
    observations: [{ text: "7011", eligible: true, token_score: .99 }],
    recognized_routes: [{ route_number: "7011", state: "recognized_single", is_target: true }],
    decisions: [{ track_id: 3, state: "recognized_single", support_samples: 1 }], ...values };
}

function clip(values = {}) {
  return { id: "server-a/session/clip_001", source_id: "server-a", session_id: "session",
    clip_key: "clip_001", device_name: "phone", note: "7011 정류장", original_url: "original.webm",
    inference_url: "inference.mp4", first_frame_offset_ms: 600,
    summary: { unique_ocr_frames: 2, frames_with_bus: 2, frames_with_text: 2, frames_with_recognition: 1 },
    samples: [sample({ recognized_routes: [], status: "searching",
      observations: [{ text: "7011", eligible: false, token_score: .8, rejection_reason: "ambiguous" }],
      decisions: [{ track_id: 3, state: "hold", support_samples: 0 }] }), sample()], ...values };
}

function harness(clips = [clip()]) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const get = id => elements.get(id);
  get("review-data").textContent = JSON.stringify({ clips, warnings: [] });
  get("speed").value = "1";
  get("global-warnings").append(new Element("div"));
  vm.runInNewContext(script, { document: { getElementById: get, createElement: tag => new Element(tag) } });
  return { get, rows: () => get("samples").children,
    buttons: row => get("samples").children[row].children[0].querySelectorAll("button") };
}

test("OCR 촬영과 응답 프레임 버튼은 각 영상의 서로 다른 대응 시각으로 이동한다", async () => {
  const app = harness();
  await app.buttons(1)[0].click();
  assert.equal(app.get("original").currentTime, 1.2);
  assert.equal(app.get("inference").currentTime, .6);
  assert.match(app.get("seek-status").textContent, /OCR 프레임 4/);
  await app.buttons(1)[1].click();
  assert.equal(app.get("original").currentTime, 2.2);
  assert.equal(app.get("inference").currentTime, 1.6);
  assert.match(app.get("seek-status").textContent, /응답 프레임 8/);
});

test("원시 OCR 문자열이 있어도 채택 근거가 없으면 채택 필터에서 제외한다", async () => {
  const app = harness();
  assert.match(app.rows()[0].children[2].textContent, /7011.*제외: ambiguous/);
  assert.equal(app.rows()[0].querySelectorAll("div.accepted").length, 0);
  assert.match(app.rows()[1].children[3].textContent, /7011 · 1회 후보/);
  app.get("only-recognized").checked = true;
  await app.get("only-recognized").emit("change");
  assert.equal(app.rows().length, 1);
  assert.match(app.rows()[0].children[3].textContent, /7011 · 1회 후보/);
});

test("기록 상태와 서버 필터 변경은 선택·재생을 갱신하고 빈 목록에서 영상을 해제한다", async () => {
  const app = harness([clip(), clip({ id: "server-b/session/clip_002", source_id: "server-b",
    clip_key: "clip_002", original_url: "second.webm", inference_url: "second.mp4",
    summary: { unique_ocr_frames: 1, frames_with_recognition: 0 }, samples: [sample({ recognized_routes: [] })] })]);
  await app.get("sync").click();
  assert.equal(app.get("original").paused, false);
  app.get("filter").value = "no-recognition";
  await app.get("filter").emit("change");
  assert.equal(app.get("original").src, "second.webm");
  assert.equal(app.get("original").paused, true);
  assert.match(app.get("title").textContent, /clip_002/);
  app.get("source").value = "server-a";
  await app.get("source").emit("change");
  assert.equal(app.get("review").hidden, true);
  assert.equal(app.get("empty").hidden, false);
  assert.equal(app.get("original").src, null);
  assert.equal(app.get("inference").src, null);
});

test("시각이 누락되면 위치 이동과 동시 재생을 비활성화하고 0ms로 표시하지 않는다", () => {
  const app = harness([clip({ first_frame_offset_ms: null,
    samples: [sample({ source_offset_ms: null, offset_ms: null, inference_video_ms: null })] })]);
  assert.equal(app.get("sync").disabled, true);
  assert.ok(app.buttons(0).every(button => button.disabled));
  assert.ok(app.buttons(0).every(button => button.textContent.includes("시각 없음")));
  assert.match(app.rows()[0].children[4].textContent, /기록 없음/);
  assert.match(app.get("seek-status").textContent, /시간 정보가 없어/);
});

test("클립 시작 전 OCR 촬영 시점은 음수 시간으로 이동하지 않는다", () => {
  const app = harness([clip({ samples: [sample({ source_offset_ms: -100 })] })]);
  assert.equal(app.buttons(0)[0].disabled, true);
  assert.match(app.rows()[0].children[0].textContent, /클립 시작 전 OCR 결과/);
  assert.equal(app.buttons(0)[1].disabled, false);
});

test("클립 종료 이후 촬영된 OCR은 영상 마지막 프레임으로 잘못 이동하지 않는다", () => {
  const app = harness([clip({ samples: [sample({ source_offset_ms: 30010, source_in_clip: false })] })]);
  assert.equal(app.buttons(0)[0].disabled, true);
  assert.match(app.rows()[0].children[0].textContent, /클립 종료 이후 OCR 결과/);
  assert.equal(app.buttons(0)[1].disabled, false);
});

test("분석 영상 시작 전 촬영을 선택하면 이전 분석 위치를 지우고 대응 프레임이 없음을 알린다", async () => {
  const app = harness([clip({ samples: [sample({ source_offset_ms: 200 })] })]);
  await app.buttons(0)[1].click();
  assert.equal(app.get("inference").currentTime, 1.6);
  await app.buttons(0)[0].click();
  assert.equal(app.get("original").currentTime, .2);
  assert.equal(app.get("inference").currentTime, 0);
  assert.match(app.get("seek-status").textContent, /분석 영상 시작.*전/);
});

test("동시 재생은 원본 시간에 맞춰 분석 영상을 보정하고 원본 일시정지를 함께 적용한다", async () => {
  const app = harness();
  app.get("original").currentTime = 2.2;
  await app.get("sync").click();
  assert.equal(app.get("original").paused, false);
  assert.equal(app.get("inference").paused, false);
  assert.ok(Math.abs(app.get("inference").currentTime - 1.6) < 1e-9);
  app.get("original").currentTime = 3;
  await app.get("original").emit("timeupdate");
  assert.equal(app.get("inference").currentTime, 2.4);
  app.get("speed").value = "2";
  await app.get("speed").emit("change");
  assert.equal(app.get("original").playbackRate, 2);
  assert.equal(app.get("inference").playbackRate, 2);
  app.get("original").pause();
  assert.equal(app.get("inference").paused, true);
});

test("이전 클립의 지연된 metadata 콜백이 새 선택 영상의 시간을 바꾸지 않는다", async () => {
  const app = harness([clip(), clip({ id: "server-b/session/clip_002", source_id: "server-b",
    clip_key: "clip_002", original_url: "second.webm" })]);
  app.get("original").readyState = 0;
  await app.buttons(1)[1].click();
  await app.get("clips").children[1].click();
  await app.get("original").emit("loadedmetadata");
  assert.equal(app.get("original").src, "second.webm");
  assert.equal(app.get("original").currentTime, 0);
});
