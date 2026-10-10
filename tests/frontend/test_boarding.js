const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

module.exports = (async () => {
  let now = 1000, current = null, view = null, calls = [], version = 1, nextFailure = null;
  const errors = [];
  let server = { status: "awaiting_stop", arrival_event_id: 1, revision: version, bus_number: null };
  const coordinator = {
    PRIORITY: { emergency: 0, boarding: 6 },
    request(request) {
      if (current && request.priority >= current.priority) return false;
      const old = current;
      current = request;
      old?.onCancel?.();
      return true;
    },
    clear(source) {
      if (current?.source !== source) return;
      const old = current;
      current = null;
      old.onCancel?.();
    },
  };
  const api = { async boarding(id, action, event, number) {
    calls.push({ id, action, event, number });
    if (nextFailure) { const failure = nextFailure; nextFailure = null; throw failure; }
    if (action === "input_ready") server.status = "pending";
    if (action === "cancel") server.status = "cancelled";
    if (action === "submit") { server.status = "submitted"; server.bus_number = number; }
    if (action === "reopen") server.status = "awaiting_stop";
    server.revision = ++version;
    return { ...server };
  } };
  const context = { window: {}, performance: { now: () => now } };
  vm.runInNewContext(fs.readFileSync("frontend/js/boarding.js", "utf8"), context);
  const guide = context.window.GBoarding.create({ api, coordinator, now: () => now,
    onChange: next => { view = next; }, onError: message => errors.push(message) });
  const accept = (state = server, extra = {}) => guide.accept({ session_id: "session", boarding: { ...state },
    stop_proximity: { nearby: true }, ...extra }, now);
  const finish = () => { const old = current; current = null; old?.onComplete?.(); };
  const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  guide.start("session");
  accept();
  assert.equal(current, null, "도착 시 정지 음성을 예약하지 않는다");
  assert.equal(calls[0].action, "input_ready");
  assert.equal(view.status, "awaiting_stop");
  await flush();
  assert.equal(view.status, "pending", "안내 음성 완료를 기다리지 않고 입력을 준비한다");
  assert.equal(current.text, context.window.GBoarding.PROMPT);
  assert.equal(current.source, "boarding");

  // An urgent hazard interrupts the question; only the pending question can resume.
  coordinator.request({ source: "walking", priority: 0, text: "멈추세요." });
  guide.tick();
  assert.equal(current.source, "walking");
  finish();
  guide.tick();
  assert.equal(current.source, "boarding");
  finish();
  guide.tick();
  assert.equal(current, null);

  // Cancellation survives missing detections and older in-flight frame responses.
  const oldPending = { ...server };
  assert.equal(await guide.cancel(), true);
  accept(oldPending, { stop_proximity: { nearby: false } });
  assert.equal(view.status, "cancelled");
  accept();
  guide.tick();
  assert.equal(current, null);
  assert.equal(await guide.reopen(), true);
  guide.tick();
  await flush();
  assert.equal(current, null, "번호 수정에서는 도착 안내를 다시 재생하지 않는다");
  assert.equal(view.status, "pending");
  assert.equal(await guide.submit("N26"), true);
  assert.equal(view.status, "submitted");
  assert.equal(view.bus_number, "N26");
  guide.stop();
  assert.equal(view, null);
  accept();
  assert.equal(view, null);

  // Crossing and stale camera results cannot open arrival input.
  server = { status: "awaiting_stop", arrival_event_id: 1, revision: ++version };
  guide.start("session");
  accept(server, { crosswalk: { event: { crossing_active: true } } });
  const callsBeforeStale = calls.length;
  assert.equal(current, null);
  assert.equal(calls.length, callsBeforeStale);
  guide.accept({ session_id: "session", boarding: server, stop_proximity: { nearby: true } }, now - 1500);
  assert.equal(current, null);
  guide.stop();
  // A manually confirmed stop can reopen without another visual stop detection.
  server = { status: "submitted", arrival_event_id: 2, revision: ++version,
    arrival_source: "user_confirmed", bus_number: "143" };
  guide.start("session");
  accept(server, { stop_proximity: { nearby: false } });
  assert.equal(await guide.reopen(), true);
  guide.tick();
  await flush();
  assert.equal(current, null);
  assert.equal(view.status, "pending");
  assert.equal(await guide.submit("271"), true);
  assert.equal(view.bus_number, "271");
  guide.stop();
  // Once arrival disables the obstacle model, automatic input can open
  // without requiring another stop detection from that suspended model.
  server = { status: "awaiting_stop", arrival_event_id: 3, revision: ++version,
    arrival_source: "visual_proximity", obstacle_detection_enabled: false };
  guide.start("session");
  accept(server, { stop_proximity: { status: "suspended", nearby: false } });
  await flush();
  assert.equal(current.text, context.window.GBoarding.PROMPT);
  assert.equal(view.status, "pending");
  guide.stop();

  // Starting speech while input_ready is in flight must consume the arrival prompt.
  server = { status: "awaiting_stop", arrival_event_id: 4, revision: ++version,
    arrival_source: "user_confirmed" };
  guide.start("session");
  accept();
  guide.dismissPrompt();
  await flush();
  assert.equal(view.status, "pending");
  guide.tick();
  assert.equal(current, null, "입력 준비 응답과 음성 입력 종료 뒤에도 도착 안내를 재시작하지 않는다");
  assert.equal(await guide.submit("143"), true);
  guide.stop();
  // A rejected submission keeps the input and its arrival token so a corrected
  // request can succeed. Only the spoken error changes; transport details stay intact.
  const failureCases = [
    [Object.assign(new Error("현재 정류장 도착에 해당하는 요청이 아닙니다."), { status: 409 }),
      "입력 상태 변경. 다시 입력."],
    [Object.assign(new Error("버스 번호는 1~30자의 문자로 입력해 주세요."), { status: 422 }),
      "번호 확인 불가. 직접 입력."],
    [Object.assign(new Error("서버 응답 시간이 초과됐습니다."), { name: "TimeoutError", stage: "timeout" }),
      "서버 응답 지연. 다시 시도."],
    [Object.assign(new TypeError("Failed to fetch"), { stage: "request", request_id: "network-test" }),
      "서버 연결 불가. 다시 시도."],
    [Object.assign(new Error("서버 내부 오류 상세"), { status: 500 }), "요청 실패. 다시 시도."],
    [new Error("알 수 없는 입력 오류"), "요청 실패. 다시 시도."],
  ];
  for (const [failure, message] of failureCases) {
    server = { status: "pending", arrival_event_id: 5, revision: ++version,
      arrival_source: "user_confirmed", bus_number: null };
    guide.start("session"); accept(); guide.dismissPrompt();
    const previous = { ...view }, originalMessage = failure.message;
    nextFailure = failure;
    assert.equal(await guide.submit("773"), false);
    assert.deepEqual({ ...view }, previous, "실패는 입력 상태와 현재 도착 요청 번호를 변경하지 않는다");
    assert.equal(errors.at(-1), message);
    assert.equal(failure.message, originalMessage, "API 진단용 오류 원문을 유지한다");
    assert.equal(await guide.submit("773"), true, "오류 후에도 같은 입력에서 다시 제출할 수 있다");
    assert.equal(calls.at(-1).event, previous.arrival_event_id);
    assert.equal(view.status, "submitted");
    assert.equal(view.bus_number, "773");
    assert.equal(view.busy, false);
    guide.stop();
  }
  console.log("boarding: pass");
})();
