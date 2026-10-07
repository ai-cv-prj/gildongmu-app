const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function harness({ reducedMotion = false } = {}) {
  const frames = new Map(), documentEvents = new Map(), motionEvents = new Map();
  let nextFrame = 0, now = 0, observer;
  const document = {
    hidden: false,
    addEventListener: (name, callback) => documentEvents.set(name, callback),
    removeEventListener: name => documentEvents.delete(name),
  };
  const motion = {
    matches: reducedMotion,
    addEventListener: (name, callback) => motionEvents.set(name, callback),
    removeEventListener: name => motionEvents.delete(name),
  };
  function canvas() {
    const element = { width: 0, height: 0, paints: 0, calls: [],
      rect: { width: 300, height: 228 }, getBoundingClientRect() { return this.rect; } };
    const context = { globalAlpha: 1 };
    for (const method of ['save', 'restore', 'translate', 'scale', 'fill', 'stroke', 'beginPath',
      'moveTo', 'lineTo', 'roundRect', 'arc', 'fillText', 'fillRect', 'setLineDash', 'bezierCurveTo', 'rect', 'clip']) {
      context[method] = (...args) => element.calls.push([method, ...args]);
    }
    context.clearRect = () => { element.calls = []; element.paints++; };
    element.getContext = () => context;
    return element;
  }
  const opening = canvas(), ending = canvas(), input = canvas(), caption = { textContent: '' };
  const context = {
    document, window: { devicePixelRatio: 3, matchMedia: () => motion },
    Path2D: class { constructor(path) { this.path = path; } },
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; this.targets = []; observer = this; }
      observe(target) { this.targets.push(target); }
      disconnect() { this.targets = []; }
    },
    requestAnimationFrame(callback) { const id = ++nextFrame; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
  };
  vm.runInNewContext(fs.readFileSync('frontend/js/film.js', 'utf8'), context);
  const film = context.GildongmuFilm.mount(opening, caption, ending, input);
  return {
    film, opening, ending, input, caption, frames, observer, documentEvents, motionEvents,
    step(ms = 100) {
      now += ms;
      const callbacks = [...frames.values()]; frames.clear();
      callbacks.forEach(callback => callback(now));
    },
    setHidden(value) { document.hidden = value; documentEvents.get('visibilitychange')?.(); },
    setMotion(value) { motion.matches = value; motionEvents.get('change')?.(); },
    resize() { observer.callback(); },
  };
}

test('장면별 그림은 표시된 캔버스만 그리며 입력 그림은 애니메이션을 예약하지 않는다', () => {
  const h = harness();
  assert.equal(h.frames.size, 0);
  assert.equal(h.observer.targets.length, 3);
  h.film.setScene('welcome');
  assert.equal(h.frames.size, 1);
  assert.equal(h.caption.textContent, '안내 시작');
  assert.equal(h.ending.paints, 0);
  assert.equal(h.input.paints, 0);
  h.step();
  const openingPaints = h.opening.paints;
  h.film.setScene('input');
  assert.equal(h.frames.size, 0);
  assert.equal(h.input.width, 600);
  assert.deepEqual(h.input.calls.filter(call => call[0] === 'fillText').map(call => call[1]), ['BUS', '143']);
  h.input.rect = { width: 420, height: 260 };
  h.resize();
  assert.equal(h.input.width, 840);
  assert.equal(h.input.height, 520);
  assert.equal(h.opening.paints, openingPaints);
  assert.equal(h.ending.paints, 0);
  h.film.setScene('camera');
  h.resize();
  assert.equal(h.input.paints, 2);
  assert.equal(h.frames.size, 0);
  h.film.destroy();
});

test('종료 장면은 하차 후 걷기를 반복하고 장면을 다시 열면 처음부터 재생한다', () => {
  const h = harness();
  h.film.setScene('end');
  assert.equal(h.frames.size, 1);
  const initial = h.ending.calls.slice();
  assert.equal(initial.filter(call => call[0] === 'fill').length, 1);
  for (let i = 0; i < 18; i++) h.step();
  assert.equal(h.ending.calls.filter(call => call[0] === 'fill').length, 2);
  h.film.setScene('end');
  assert.equal(h.frames.size, 1);
  for (let i = 0; i < 75; i++) h.step();
  assert.equal(h.ending.calls.filter(call => call[0] === 'fill').length, 1);
  h.film.setScene('input');
  assert.equal(h.frames.size, 0);
  h.film.setScene('end');
  assert.deepEqual(h.ending.calls, initial);
  assert.equal(h.frames.size, 1);
  assert.equal(h.opening.paints, 0);
  h.film.destroy();
});

test('동작 줄이기에서는 종료 장면을 정지 그림으로 유지하고 설정 변경을 반영한다', () => {
  const h = harness({ reducedMotion: true });
  h.film.setScene('end');
  const still = h.ending.calls.slice();
  assert.equal(still.filter(call => call[0] === 'fill').length, 2);
  assert.equal(h.frames.size, 0);
  h.resize();
  assert.deepEqual(h.ending.calls, still);
  h.setMotion(false);
  assert.equal(h.frames.size, 1);
  for (let i = 0; i < 8; i++) h.step();
  h.setMotion(true);
  assert.equal(h.frames.size, 0);
  assert.deepEqual(h.ending.calls, still);
  h.film.destroy();
});

test('숨겨진 문서와 제거된 화면은 재생을 중단하고 기존 setVisible API를 지원한다', () => {
  const h = harness();
  h.film.setVisible(true);
  assert.equal(h.frames.size, 1);
  h.film.setVisible(false);
  assert.equal(h.frames.size, 0);
  h.film.setScene('end');
  h.step();
  h.setHidden(true);
  const paints = h.ending.paints;
  assert.equal(h.frames.size, 0);
  h.resize();
  assert.equal(h.ending.paints, paints);
  h.setHidden(false);
  assert.equal(h.frames.size, 1);
  h.film.destroy();
  assert.equal(h.frames.size, 0);
  assert.equal(h.observer.targets.length, 0);
  assert.equal(h.documentEvents.size, 0);
  assert.equal(h.motionEvents.size, 0);
  h.film.setScene('welcome');
  h.film.restart();
  h.resize();
  assert.equal(h.frames.size, 0);
});
