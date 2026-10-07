const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

// Small DOM harness: exercise the shipped handlers without exporting private credentials/state.
class Node {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.attributes = {};
    this.className = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this._text = "";
    this.classList = { toggle: (name, enabled) => {
      const values = new Set(this.className.split(/\s+/).filter(Boolean));
      if (enabled) values.add(name); else values.delete(name);
      this.className = [...values].join(" ");
    } };
  }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  get isConnected() { return this.tagName === "body" || Boolean(this.parentNode?.isConnected); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.tagName === "fragment") { this.append(...node.children); continue; }
      node.parentNode = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) {
    const handlers = this.listeners.get(name) || [];
    handlers.push(callback);
    this.listeners.set(name, handlers);
  }
  fire(name, fields = {}) {
    return Promise.all((this.listeners.get(name) || []).map(handler => handler({ preventDefault() {}, ...fields })));
  }
  querySelectorAll(selector) {
    const result = [];
    for (const child of this.children) {
      if (selector.startsWith(".") ? child.className.split(/\s+/).includes(selector.slice(1)) : child.tagName === selector) result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
}
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
const response = (body = {}, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function harness({ adminEnabled = true } = {}) {
  const body = new Node("body"), nodes = new Map(), timers = new Map();
  let timerId = 0;
  const html = fs.readFileSync("result_hub/static/index.html", "utf8");
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Node(match[1]);
    node.hidden = /\shidden\b/.test(match[0]);
    node.disabled = /\sdisabled\b/.test(match[0]);
    node.className = /class="([^"]*)"/.exec(match[0])?.[1] || "";
    nodes.set(match[2], node);
    body.append(node);
  }
  const calls = [], confirmations = [];
  const server = {
    sessions: [
      { source_id: "member1", session_id: "same-id", device_name: "A", started_at: "2026-10-07T10:00:00Z" },
      { source_id: "member2", session_id: "same-id", device_name: "B", started_at: "2026-10-07T09:00:00Z" },
      { source_id: "member2", session_id: "older", device_name: "C", started_at: "2026-10-06T09:00:00Z" },
    ],
    logs: [
      { source_id: "member1", path: "logs/app.log", size: 20 },
      { source_id: "member1", path: "logs/app.log.2026-10-06", size: 50 },
    ],
    trash: [], intercept: null, confirm: true,
  };
  for (const session of server.sessions) session.clips = [
    { clip_id: 1, category_key: "clip_001", categories: [], category_revision: 0, state: "ready" },
    { clip_id: 2, category_key: "clip_002", categories: [], category_revision: 0, state: "ready" },
    { clip_id: 3, category_key: "clip_003", categories: [], category_revision: 0, state: "ready" },
  ];
  const document = {
    body, hidden: false,
    getElementById: id => { assert.ok(nodes.has(id), `Unknown DOM id ${id}`); return nodes.get(id); },
    createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"),
    querySelectorAll: selector => body.querySelectorAll(selector), addEventListener() {},
  };
  const context = {
    document, URL, URLSearchParams, AbortController, Blob, Date, Intl, console,
    location: { origin: "https://hub.test" },
    Option: function Option(text, value) { const node = new Node("option"); node.textContent = text; node.value = value; return node; },
    setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), setInterval() {},
    localStorage: { setItem() { assert.fail("Credentials must not be persisted"); } },
    sessionStorage: { setItem() { assert.fail("Credentials must not be persisted"); } },
    confirm(message) { confirmations.push(message); return server.confirm; },
    async fetch(path, options) {
      calls.push({ path, ...options });
      if (server.intercept) { const result = await server.intercept(path, options); if (result) return result; }
      const url = new URL(path, "https://hub.test");
      if (url.pathname === "/api/capabilities") return response({ admin_enabled: adminEnabled });
      if (url.pathname.startsWith("/api/admin/")) {
        if (options.headers["X-Hub-Admin-Password"] !== "admin-secret") return response({ detail: "관리자 비밀번호가 올바르지 않습니다." }, 403);
        if (url.pathname === "/api/admin/login") return response({ ok: true });
        if (url.pathname === "/api/admin/trash") return response({ items: server.trash });
        const sessionMatch = /^\/api\/admin\/sessions\/([^/]+)\/([^/]+)$/.exec(url.pathname);
        const logMatch = /^\/api\/admin\/logs\/([^/]+)\/(.+)$/.exec(url.pathname);
        if (sessionMatch || logMatch) {
          const source = (sessionMatch || logMatch)[1], target = (sessionMatch || logMatch)[2];
          const kind = sessionMatch ? "session" : "log";
          const data = sessionMatch ? server.sessions : server.logs;
          const index = data.findIndex(item => item.source_id === source && (sessionMatch ? item.session_id : item.path) === target);
          if (index < 0) return response({ detail: "기록이 없습니다." }, 404);
          const [saved] = data.splice(index, 1);
          const item = { id: `trash-${server.trash.length + 1}`, kind, source_id: source, path: target, file_count: 2, size: 50, saved };
          server.trash.push(item);
          return response({ ok: true, item });
        }
        const restoreMatch = /^\/api\/admin\/trash\/([^/]+)\/restore$/.exec(url.pathname);
        if (restoreMatch) {
          const index = server.trash.findIndex(item => item.id === restoreMatch[1]);
          const [item] = server.trash.splice(index, 1);
          (item.kind === "session" ? server.sessions : server.logs).push(item.saved);
          return response({ ok: true, item });
        }
      }
      if (url.pathname === "/api/sources") return response({ sources: ["member1", "member2"].map(source_id => ({ source_id })) });
      if (url.pathname === "/api/sessions") {
        const category = url.searchParams.get("category");
        const rows = server.sessions.map(item => ({ ...item,
          categories: ["obstacle", "traffic_light", "bus"].filter(value => item.clips.some(clip => clip.categories.includes(value))),
          unclassified_clip_count: item.clips.filter(clip => !clip.categories.length).length,
          matched_clip_count: item.clips.filter(clip => !category || (category === "unclassified" ? !clip.categories.length : clip.categories.includes(category))).length,
        }));
        return response({ sessions: rows.filter(item => (!category || item.matched_clip_count) && (!url.searchParams.get("source_id") || item.source_id === url.searchParams.get("source_id")) && (!url.searchParams.get("q") || item.device_name.includes(url.searchParams.get("q")))) });
      }
      if (url.pathname === "/api/logs") return response({ logs: server.logs.filter(item => !url.searchParams.get("source_id") || item.source_id === url.searchParams.get("source_id")) });
      if (url.pathname.startsWith("/api/sessions/")) {
        const [, , , source_id, session_id] = url.pathname.split("/");
        const item = server.sessions.find(item => item.source_id === source_id && item.session_id === session_id);
        if (url.pathname.endsWith("/categories")) {
          assert.equal(options.method, "PUT");
          assert.equal(options.headers["X-Hub-Request"], "categories");
          assert.equal(options.headers["X-Hub-Admin-Password"], undefined);
          const clip = item.clips.find(clip => clip.category_key === url.pathname.split("/")[6]);
          assert.ok(clip, "mutation must identify a specific clip");
          const payload = JSON.parse(options.body);
          if (payload.revision !== (clip.category_revision || 0)) return response({ detail: "다른 팀원이 분류를 변경했습니다. 상세 새로고침 후 다시 선택해 주세요." }, 409);
          clip.categories = payload.categories;
          clip.category_revision = payload.revision + 1;
          return response({ ok: true, item: { category_key: clip.category_key, categories: clip.categories, category_revision: clip.category_revision } });
        }
        return response({ source_id, session_id, session: {}, clips: item.clips.map(clip => ({ ...clip, categories: [...clip.categories] })), files: [] });
      }
      if (url.pathname.startsWith("/api/preview/")) return response({ text: "test log" });
      assert.fail(`Unexpected request ${path}`);
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync("result_hub/static/app.js", "utf8"), context);
  const $ = id => nodes.get(id);
  return {
    $, calls, server, confirmations, document,
    async login(password = "admin-secret") { await $("admin-toggle").fire("click"); $("admin-password").value = password; return $("admin-form").fire("submit"); },
    async search(value) { $("search").value = value; await $("search").fire("input"); for (const [id, timer] of timers) if (timer.delay === 300) { timers.delete(id); timer.fn(); } await settle(); },
    async selectAll() { $("select-all").checked = true; await $("select-all").fire("change"); },
  };
}

test("viewer requires separate admin authentication; password stays out of ordinary requests and is cleared on logout/reload", async () => {
  const h = harness(); await settle();
  assert.equal(h.$("selection-toolbar").hidden, true);
  assert.equal(h.$("trash-tab").hidden, true);
  assert.equal(h.calls.some(call => call.path.startsWith("/api/admin/")), false);
  await h.login("wrong");
  assert.equal(h.$("admin-password").value, "");
  assert.match(h.$("admin-login-error").textContent, /비밀번호가 올바르지/);
  assert.equal(h.$("selection-toolbar").hidden, true);
  await h.login();
  assert.equal(h.$("selection-toolbar").hidden, false);
  assert.equal(h.$("admin-password").value, "");
  await h.$("trash-tab").fire("click"); await settle();
  assert.equal(h.calls.find(call => call.path === "/api/admin/trash").headers["X-Hub-Admin-Password"], "admin-secret");
  assert.ok(h.calls.filter(call => !call.path.startsWith("/api/admin/")).every(call => !("X-Hub-Admin-Password" in call.headers)));
  await h.$("admin-toggle").fire("click");
  assert.equal(h.$("trash-panel").hidden, true);
  assert.equal(h.$("selection-toolbar").hidden, true);
  assert.equal(h.$("trash-list").children.length, 0);
  const reloaded = harness(); await settle();
  assert.equal(reloaded.$("selection-toolbar").hidden, true);
});

test("cancelled login cannot reactivate admin mode when its response arrives late", async () => {
  const h = harness(); await settle();
  const gate = deferred();
  h.server.intercept = path => path === "/api/admin/login" ? gate.promise : undefined;
  const login = h.login(); await settle();
  assert.equal(h.$("admin-password").value, "");
  await h.$("admin-cancel").fire("click");
  gate.resolve(response({ ok: true })); await login;
  assert.equal(h.$("selection-toolbar").hidden, true);
  assert.equal(h.$("admin-dialog").open, false);
});

test("batch deletion is sequential, prevents double submit, clears deleted detail and stops on a partial failure", async () => {
  const h = harness(); await settle(); await h.login();
  await h.$("session-list").querySelector(".session-item").fire("click");
  await h.selectAll();
  const gate = deferred(); let deletes = 0;
  h.server.intercept = async (path, options) => {
    if (options.method !== "DELETE") return;
    if (++deletes === 1) { await gate.promise; return; }
    return response({ detail: "수집 중인 기록입니다." }, 409);
  };
  const pending = h.$("delete-selected").fire("click"); await settle();
  assert.equal(deletes, 1);
  assert.equal(h.$("delete-selected").disabled, true);
  await h.$("delete-selected").fire("click");
  assert.equal(deletes, 1);
  assert.match(h.confirmations[0], /member1 \/ same-id/);
  assert.match(h.confirmations[0], /member2 \/ same-id/);
  assert.match(h.confirmations[0], /자동 재업로드는 차단/);
  gate.resolve(); await pending;
  assert.equal(deletes, 2);
  assert.equal(h.server.sessions.length, 2);
  assert.match(h.$("admin-status").textContent, /1 \/ 3개/);
  assert.match(h.$("admin-status").textContent, /수집 중인 기록입니다/);
  assert.match(h.$("session-detail").textContent, /테스트 기록을 선택하세요/);
  assert.equal(h.$("selection-count").textContent, "2개 선택");
  assert.ok(h.calls.filter(call => call.method === "DELETE").every(call => call.headers["X-Hub-Admin-Password"] === "admin-secret" && call.body === undefined));
});

test("filter changes clear hidden selections; failed filtering never leaves old records deletable", async () => {
  const h = harness(); await settle(); await h.login(); await h.selectAll();
  await h.search("B");
  assert.equal(h.$("selection-count").textContent, "0개 선택");
  assert.equal(h.$("session-list").querySelectorAll(".session-item").length, 1);
  await h.selectAll();
  h.server.confirm = false;
  await h.$("delete-selected").fire("click");
  assert.equal(h.calls.filter(call => call.method === "DELETE").length, 0);
  assert.match(h.confirmations[0], /member2 \/ same-id/);
  assert.doesNotMatch(h.confirmations[0], /member1/);
  h.server.intercept = path => path.startsWith("/api/sessions?") ? response({ detail: "목록 오류" }, 500) : undefined;
  await h.search("A");
  assert.equal(h.$("delete-selected").disabled, true);
  assert.equal(h.$("session-list").querySelectorAll(".session-item").length, 0);
  assert.match(h.$("notice").textContent, /목록 오류/);
});

test("only archived logs offer deletion; restore works and stale controls do nothing after logout", async () => {
  const h = harness(); await settle(); await h.login();
  await h.$("log-list").querySelectorAll(".log-item")[0].fire("click");
  assert.equal(h.$("log-detail").querySelector(".danger-button"), null);
  await h.$("log-list").querySelectorAll(".log-item")[1].fire("click");
  await h.$("log-detail").querySelector(".danger-button").fire("click");
  assert.equal(h.server.logs.length, 1);
  assert.equal(h.server.logs[0].path, "logs/app.log");
  assert.match(h.$("log-detail").textContent, /로그 파일을 선택하세요/);
  await h.$("trash-tab").fire("click"); await settle();
  const restore = h.$("trash-list").querySelector("button");
  await restore.fire("click");
  assert.equal(h.server.logs.length, 2);
  assert.equal(h.server.trash.length, 0);
  await h.$("admin-toggle").fire("click");
  const before = h.calls.length;
  await restore.fire("click");
  assert.equal(h.calls.length, before);
});

test("rejected admin credentials remove privileges and stop remaining batch requests", async () => {
  const h = harness(); await settle(); await h.login(); await h.selectAll();
  h.server.intercept = (path, options) => options.method === "DELETE" ? response({ detail: "관리자 인증이 만료되었습니다." }, 403) : undefined;
  await h.$("delete-selected").fire("click");
  assert.equal(h.calls.filter(call => call.method === "DELETE").length, 1);
  assert.equal(h.$("selection-toolbar").hidden, true);
  assert.equal(h.$("trash-tab").hidden, true);
  assert.match(h.$("admin-status").textContent, /관리자 인증이 만료/);
  assert.equal(h.server.sessions.length, 3);
});


test("ordinary teammates save multiple categories and find a record under either category", async () => {
  const h = harness({ adminEnabled: false }); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  const form = h.$("session-detail").querySelector(".category-editor");
  const inputs = form.querySelectorAll("input");
  inputs[0].checked = true; await inputs[0].fire("change");
  inputs[2].checked = true; await inputs[2].fire("change");
  await form.fire("submit");
  assert.deepEqual(h.server.sessions[0].clips[0].categories, ["obstacle", "bus"]);
  assert.match(form.textContent, /분류를 저장했습니다/);
  assert.match(h.$("session-list").textContent, /장애물버스/);
  assert.equal(h.calls.some(call => call.path.startsWith("/api/admin/")), false);
  for (const category of ["obstacle", "bus", "traffic_light", "unclassified"]) {
    h.$("category-filter").value = category;
    await h.$("category-filter").fire("change"); await settle();
    assert.equal(h.$("session-list").querySelectorAll(".session-item").length,
      category === "unclassified" ? 3 : category === "traffic_light" ? 0 : 1);
  }
});

test("failed saves keep the draft; conflicts require reload and never overwrite teammates", async () => {
  const h = harness(); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  let form = h.$("session-detail").querySelector(".category-editor");
  let input = form.querySelectorAll("input")[2];
  input.checked = true; await input.fire("change");
  h.server.intercept = (path, options) => options.method === "PUT" ? response({ detail: "저장 실패" }, 503) : undefined;
  await form.fire("submit");
  assert.match(form.textContent, /저장 실패/);
  assert.equal(input.checked, true);
  assert.equal(form.querySelector("button").disabled, false);
  h.server.intercept = null;
  h.server.sessions[0].clips[0].categories = ["traffic_light"];
  h.server.sessions[0].clips[0].category_revision = 1;
  await form.fire("submit");
  assert.match(form.textContent, /다른 팀원/);
  assert.deepEqual(h.server.sessions[0].clips[0].categories, ["traffic_light"]);
  const reload = h.$("session-detail").querySelectorAll("button").find(node => node.textContent === "상세 새로고침");
  await reload.fire("click"); await settle();
  form = h.$("session-detail").querySelector(".category-editor");
  const inputs = form.querySelectorAll("input");
  assert.equal(inputs[1].checked, true);
  for (const node of inputs) { node.checked = false; await node.fire("change"); }
  await form.fire("submit");
  assert.deepEqual(h.server.sessions[0].clips[0].categories, []);
  assert.equal(h.server.sessions[0].clips[0].category_revision, 2);
});

test("saving a previous selection cannot assign its categories to another record", async () => {
  const h = harness(); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  const form = h.$("session-detail").querySelector(".category-editor");
  const input = form.querySelectorAll("input")[2]; input.checked = true; await input.fire("change");
  const gate = deferred();
  h.server.intercept = (path, options) => options.method === "PUT" ? gate.promise : undefined;
  const saving = form.fire("submit"); await settle();
  await h.$("session-list").querySelectorAll(".session-item")[1].fire("click");
  gate.resolve(response({ ok: true, item: { category_key: "clip_001", categories: ["bus"], category_revision: 1 } }));
  await saving;
  const nextForm = h.$("session-detail").querySelector(".category-editor");
  assert.equal(nextForm.querySelectorAll("input")[2].checked, false);
  assert.equal(nextForm.querySelector("button").disabled, false);
});


test("mixed clips save independently and filtering hides nonmatching clips within the same session", async () => {
  const h = harness({ adminEnabled: false }); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  const forms = h.$("session-detail").querySelectorAll(".category-editor");
  assert.equal(forms.length, 3);
  const choices = [[2], [1], [0, 2]];
  for (let i = 0; i < forms.length; i++) {
    const inputs = forms[i].querySelectorAll("input");
    for (const index of choices[i]) { inputs[index].checked = true; await inputs[index].fire("change"); }
    await forms[i].fire("submit");
  }
  assert.deepEqual(h.server.sessions[0].clips.map(clip => clip.categories), [["bus"], ["traffic_light"], ["obstacle", "bus"]]);
  assert.equal(h.server.sessions[1].clips[0].categories.length, 0);
  h.$("category-filter").value = "bus";
  await h.$("category-filter").fire("change"); await settle();
  assert.match(h.$("session-list").textContent, /선택한 종류의 클립 2개/);
  await h.$("session-list").querySelector("button").fire("click");
  let cards = h.$("session-detail").querySelectorAll(".clip-card");
  assert.deepEqual(cards.map(card => card.hidden), [false, true, false]);
  assert.match(h.$("session-detail").querySelector(".clip-filter-note").textContent, /버스 클립 2개/);
  // Removing a matching category hides just that clip without changing its siblings.
  const busForm = cards[0].querySelector(".category-editor");
  const bus = busForm.querySelectorAll("input")[2]; bus.checked = false; await bus.fire("change");
  await busForm.fire("submit");
  assert.equal(cards[0].hidden, true);
  assert.equal(cards[2].hidden, false);
  assert.match(h.$("session-detail").querySelector(".clip-filter-note").textContent, /버스 클립 1개/);
  assert.deepEqual(h.server.sessions[0].clips[2].categories, ["obstacle", "bus"]);
  h.$("category-filter").value = "unclassified";
  await h.$("category-filter").fire("change"); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  cards = h.$("session-detail").querySelectorAll(".clip-card");
  assert.deepEqual(cards.map(card => card.hidden), [false, true, true]);
});


test("unsaved sibling clip choices survive saving another clip and toggling admin mode", async () => {
  const h = harness(); await settle();
  await h.$("session-list").querySelector("button").fire("click");
  let forms = h.$("session-detail").querySelectorAll(".category-editor");
  const pending = forms[1].querySelectorAll("input")[1]; pending.checked = true; await pending.fire("change");
  const bus = forms[0].querySelectorAll("input")[2]; bus.checked = true; await bus.fire("change");
  await forms[0].fire("submit");
  await h.login();
  forms = h.$("session-detail").querySelectorAll(".category-editor");
  assert.equal(forms[1].querySelectorAll("input")[1].checked, true);
  await forms[1].fire("submit");
  assert.deepEqual(h.server.sessions[0].clips.map(clip => clip.categories), [["bus"], ["traffic_light"], []]);
});
