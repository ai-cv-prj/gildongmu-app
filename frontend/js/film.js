/* 길동무 오프닝 · 28초 · 참고 이미지에 맞춘 단색 윤곽 일러스트
 * 보행 → 음성 안내를 듣고 사람 회피 → 빨간불 대기 → 초록불 횡단
 * → 정류장 → 음성으로 143번 입력·확인 → 버스 발견·탑승 → 다음 루프
 * 각 장소는 해당 단계에서 처음 등장합니다. 인물·버스는 실제 일러스트 프레임입니다.
 */
(() => {
  'use strict';
  const DURATION = 28;
  const INK = '#242424';
  const source = {
  "hero": {
    "width": 1448,
    "height": 1086,
    "frames": [
      {
        "sx": 112,
        "sy": 25,
        "sw": 208,
        "sh": 334,
        "anchor": 114.5
      },
      {
        "sx": 467,
        "sy": 23,
        "sw": 153,
        "sh": 334,
        "anchor": 90.0
      },
      {
        "sx": 807,
        "sy": 23,
        "sw": 176,
        "sh": 334,
        "anchor": 96.0
      },
      {
        "sx": 1171,
        "sy": 23,
        "sw": 196,
        "sh": 334,
        "anchor": 94.5
      },
      {
        "sx": 119,
        "sy": 383,
        "sw": 213,
        "sh": 335,
        "anchor": 114.0
      },
      {
        "sx": 485,
        "sy": 380,
        "sw": 135,
        "sh": 338,
        "anchor": 75.5
      },
      {
        "sx": 812,
        "sy": 380,
        "sw": 195,
        "sh": 338,
        "anchor": 94.5
      },
      {
        "sx": 1170,
        "sy": 380,
        "sw": 214,
        "sh": 337,
        "anchor": 98.5
      },
      {
        "sx": 156,
        "sy": 736,
        "sw": 120,
        "sh": 334,
        "anchor": 58.5
      },
      {
        "sx": 491,
        "sy": 736,
        "sw": 118,
        "sh": 334,
        "anchor": 58.5
      },
      {
        "sx": 830,
        "sy": 745,
        "sw": 129,
        "sh": 325,
        "anchor": 78.0
      },
      {
        "sx": 1177,
        "sy": 736,
        "sw": 184,
        "sh": 334,
        "anchor": 85.0
      }
    ],
    "src": "/static/assets/film/hero.webp"
  },
  "passer": {
    "width": 1263,
    "height": 1246,
    "frames": [
      {
        "sx": 168,
        "sy": 37,
        "sw": 360,
        "sh": 586,
        "anchor": 198.0
      },
      {
        "sx": 785,
        "sy": 36,
        "sw": 273,
        "sh": 587,
        "anchor": 118.5
      },
      {
        "sx": 168,
        "sy": 645,
        "sw": 367,
        "sh": 583,
        "anchor": 201.0
      },
      {
        "sx": 793,
        "sy": 623,
        "sw": 284,
        "sh": 607,
        "anchor": 90.5
      }
    ],
    "src": "/static/assets/film/passer.webp"
  },
  "bus": {
    "width": 1774,
    "height": 887,
    "frames": [
      {
        "sx": 17,
        "sy": 303,
        "sw": 855,
        "sh": 324,
        "anchor": 0
      },
      {
        "sx": 904,
        "sy": 303,
        "sw": 856,
        "sh": 324,
        "anchor": 0
      }
    ],
    "src": "/static/assets/film/bus.webp"
  }
};
  let images = {};
  const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
  const mix = (a, b, p) => a + (b - a) * p;
  const span = (t, a, b) => clamp((t - a) / (b - a));
  const ease = n => { n = clamp(n); return n * n * (3 - 2 * n); };
  const easeOut = n => 1 - Math.pow(1 - clamp(n), 3);
  function setImages(value) { images = value; }
  function line(c, points, width = 1.7, color = INK) {
    c.save(); c.strokeStyle = color; c.lineWidth = width; c.lineCap = 'round'; c.lineJoin = 'round';
    c.beginPath(); c.moveTo(points[0][0], points[0][1]);
    for (let i = 1; i < points.length; i++) c.lineTo(points[i][0], points[i][1]);
    c.stroke(); c.restore();
  }
  function round(c, x, y, w, h, r = 10, width = 1.7, color = INK) {
    c.save(); c.strokeStyle = color; c.lineWidth = width; c.lineCap = 'round';
    c.beginPath(); c.roundRect(x, y, w, h, r); c.stroke(); c.restore();
  }
  function arc(c, x, y, radius, a = 0, b = Math.PI * 2, width = 1.7, color = INK) {
    c.save(); c.strokeStyle = color; c.lineWidth = width; c.lineCap = 'round';
    c.beginPath(); c.arc(x, y, radius, a, b); c.stroke(); c.restore();
  }
  function text(c, value, x, y, size = 24, weight = 600, align = 'center') {
    c.save(); c.fillStyle = INK; c.font = weight + ' ' + size + 'px sans-serif';
    c.textAlign = align; c.fillText(value, x, y); c.restore();
  }

  // 프레임의 머리 중심과 발 바닥을 맞춰, 인물이 떨리거나 크기가 바뀌지 않게 합니다.
  function actor(c, kind, frame, headX, feet, height, alpha = 1) {
    const img = images[kind]; if (!img) return;
    const f = source[kind].frames[frame];
    const scale = height / f.sh;
    const x = headX - f.anchor * scale;
    c.save(); c.globalAlpha *= alpha; c.filter = 'grayscale(1)';
    c.drawImage(img, f.sx, f.sy, f.sw, f.sh, x, feet - height, f.sw * scale, height);
    c.restore();
  }
  function heroFrame(t, walking, speaking = false, boarding = false) {
    if (boarding) return 11;
    if (speaking) return 10;
    return walking ? Math.floor(t * 9) % 8 : 8;
  }
  function ground(c) {
    line(c, [[-70, 398], [670, 398]], 1.15, '#A9A9A9');
    line(c, [[0, 413], [600, 413]], .8, '#D2D2D2');
  }
  function cameraCue(c, x, y, amount) {
    c.save(); c.globalAlpha *= amount * .8;
    [22, 31].forEach(r => arc(c, x, y, r, -.46, .4, 1.5));
    c.restore();
  }

  function street(c, t) {
    ground(c);
    // 첫 화면에는 길과 주인공만 있습니다.
    let x = mix(125, 238, ease(span(t, 0, 2.4)));
    let feet = 394, height = 265;
    let walking = t > .18 && t < 2.4;
    if (t >= 2.4 && t < 2.9) walking = false;
    if (t >= 2.9) {
      const p = ease(span(t, 2.9, 4.9));
      x = mix(238, 375, p);
      const aside = Math.sin(p * Math.PI);
      feet = 394 - 58 * aside; height = 265 - 24 * aside;
      walking = true;
    }
    if (t > 4.9) x = mix(375, 624, ease(span(t, 4.9, 6.35)));
    actor(c, 'hero', heroFrame(t, walking), x, feet, height);
    if (t >= 1.75 && t < 6.35) {
      const otherX = mix(685, -120, span(t, 1.75, 6.35));
      actor(c, 'passer', Math.floor(t * 7) % 4, otherX, 397, 262);
    }
    if (t > 2.25 && t < 2.9) cameraCue(c, x + 17, feet - height * .66, Math.sin(span(t, 2.25, 2.9) * Math.PI));
    // 짧은 곡선으로 우회한 방향만 보여줍니다.
    if (t > 3.05 && t < 5.1) {
      c.save(); c.globalAlpha *= .24 * Math.sin(span(t, 3.05, 5.1) * Math.PI);
      c.strokeStyle = INK; c.lineWidth = 1.2; c.setLineDash([4, 7]);
      c.beginPath(); c.moveTo(238, 395); c.bezierCurveTo(275, 316, 342, 317, 375, 395); c.stroke(); c.restore();
    }
  }

  function signal(c, walking) {
    const x = 331, y = 117;
    line(c, [[x + 24, y + 107], [x + 24, 398]], 2.1);
    round(c, x, y, 48, 108, 14, 2);
    // 신호 색과 사람 모양을 함께 표시합니다.
    const gray = '#B4B4B4';
    const red = walking ? gray : '#D92D38';
    const green = walking ? '#00864A' : gray;
    c.save(); c.fillStyle = walking ? '#FFF4BE' : '#D92D38';
    c.beginPath(); c.roundRect(x + 5, y + 7, 38, 42, 9); c.fill();
    c.fillStyle = walking ? '#00864A' : '#FFF4BE';
    c.beginPath(); c.roundRect(x + 5, y + 58, 38, 42, 9); c.fill(); c.restore();
    round(c, x + 5, y + 7, 38, 42, 9, walking ? 1 : 2.8, red);
    round(c, x + 5, y + 58, 38, 42, 9, walking ? 2.8 : 1, green);
    arc(c, x + 24, y + 17, 3.2, 0, Math.PI * 2, 1.4, walking ? gray : '#FFFFFF');
    line(c, [[x + 24, y + 23], [x + 24, y + 34], [x + 19, y + 42]], 1.8, walking ? gray : '#FFFFFF');
    line(c, [[x + 24, y + 34], [x + 29, y + 42]], 1.8, walking ? gray : '#FFFFFF');
    line(c, [[x + 17, y + 28], [x + 31, y + 28]], 1.8, walking ? gray : '#FFFFFF');
    arc(c, x + 26, y + 68, 3.2, 0, Math.PI * 2, 1.4, walking ? '#FFFFFF' : gray);
    line(c, [[x + 25, y + 74], [x + 22, y + 84], [x + 15, y + 92]], 1.8, walking ? '#FFFFFF' : gray);
    line(c, [[x + 22, y + 84], [x + 30, y + 93]], 1.8, walking ? '#FFFFFF' : gray);
    line(c, [[x + 20, y + 78], [x + 15, y + 83]], 1.8, walking ? '#FFFFFF' : gray);
    line(c, [[x + 22, y + 77], [x + 31, y + 82]], 1.8, walking ? '#FFFFFF' : gray);
    text(c, walking ? 'WALK' : 'STOP', x + 24, y - 17, 20, 700);
  }
  function crossing(c, t) {
    ground(c);
    // 횡단보도는 이 장면에서 처음 등장합니다.
    line(c, [[287, 398], [271, 445]], 1.7, '#737373');
    for (let i = 0; i < 7; i++) {
      const x = 302 + i * 44;
      line(c, [[x, 407], [x + 20, 407], [x + 4, 438], [x - 16, 438], [x, 407]], 1.4, '#8A8A8A');
    }
    const green = t >= 10.15;
    signal(c, green);
    let x = mix(120, 224, ease(span(t, 6.4, 7.6)));
    let walking = t < 7.6;
    let feet = 394;
    if (green) {
      x = mix(224, 678, ease(span(t, 10.35, 13.8)));
      feet = mix(394, 417, ease(span(t, 10.35, 11.35))); walking = t > 10.35;
    }
    actor(c, 'hero', heroFrame(t, walking), x, feet, 263);
  }

  function busStop(c) {
    line(c, [[324, 137], [324, 398]], 2);
    round(c, 293, 66, 62, 74, 16, 2);
    text(c, 'BUS', 324, 92, 15, 600);
    round(c, 307, 101, 34, 21, 5, 1.7);
    line(c, [[308, 111], [340, 111]], 1.2);
    arc(c, 313, 124, 2.5, 0, Math.PI * 2, 1.6);
    arc(c, 335, 124, 2.5, 0, Math.PI * 2, 1.6);
  }
  function bubble(c, x, y, w, value, pointed = true, wave = false, check = false) {
    round(c, x, y, w, 60, 18, 1.8);
    if (pointed) line(c, [[x + w * .65, y + 60], [x + w * .68, y + 74], [x + w * .77, y + 61]], 1.6);
    text(c, value, x + w / 2 - (check ? 12 : 0), y + 40, 32, 600);
    if (check) line(c, [[x + w - 36, y + 29], [x + w - 29, y + 37], [x + w - 17, y + 23]], 2.4);
    if (wave) for (let i = 0; i < 5; i++) {
      const h = [5, 9, 13, 9, 5][i];
      line(c, [[x + w / 2 - 16 + i * 8, y - 8 - h], [x + w / 2 - 16 + i * 8, y - 8]], 1.7);
    }
  }

  function drawBus(c, x, floor, width, open, boarded) {
    const img = images.bus; if (!img) return;
    const f = source.bus.frames[open ? 1 : 0];
    const height = width * f.sh / f.sw, y = floor - height;
    c.save(); c.filter = 'grayscale(1)';
    c.drawImage(img, f.sx, f.sy, f.sw, f.sh, x, y, width, height);
    c.restore();
    if (boarded) {
      // 창문 안에 같은 가슴 카메라를 착용한 인물이 보입니다.
      c.save(); const wx = x + width * .268, wy = y + height * .217;
      c.beginPath(); c.rect(wx, wy, width * .152, height * .311); c.clip();
      actor(c, 'hero', 8, x + width * .327, wy + 181, 170);
      c.restore();
    }
  }
  function station(c, t) {
    const cameraPan = 100 * ease(span(t, 22.05, 24.75));
    c.save(); c.translate(-cameraPan, 0);
    ground(c); busStop(c);
    let x = mix(90, 230, ease(span(t, 13.8, 15.35)));
    let height = 260, feet = 394;
    const speaking = t >= 15.8 && t < 18.05;
    let walking = t < 15.35;
    let boarded = t >= 24.8;
    let bx = mix(668, 330, easeOut(span(t, 19.3, 21.9)));
    if (t > 25.35) bx = mix(330, -1110, ease(span(t, 25.35, 27.35)));
    const open = t >= 22.05 && t < 25.15;
    if (t >= 19.3) drawBus(c, bx, 397, 900, open, boarded);
    if (t >= 22.15) {
      x = mix(230, 500, ease(span(t, 22.15, 24.8)));
      const step = ease(span(t, 23.9, 24.8));
      height = mix(260, 184, step); feet = mix(394, 352, step);
      walking = t < 24;
    }
    if (!boarded) {
      c.save();
      if (t > 24.25) { c.beginPath(); c.rect(-200, -200, bx + 215 + 200, 900); c.clip(); }
      actor(c, 'hero', heroFrame(t, walking, speaking, t >= 24), x, feet, height);
      c.restore();
    }
    // 모든 번호 표시를 인물 바로 위에 둡니다. 정류장 표지와 겹치지 않습니다.
    if (t >= 15.9 && t < 17.25) bubble(c, x - 86, feet - height - 90, 132, '143', true, true);
    if (t >= 17.25 && t < 18.25) bubble(c, x - 86, feet - height - 90, 132, '143?', true, false);
    if (t >= 18.25 && t < 19.25) bubble(c, x - 92.5, feet - height - 90, 145, '143', true, false, true);
    c.restore();
  }

  function description(t) {
    if (t >= 27.65) return '가슴에 고정한 카메라로';
    if (t < 2.25) return '가슴에 고정한 카메라로';
    if (t < 5.1) return '“왼쪽으로 이동하세요.”';
    if (t < 6.4) return '음성 안내를 따라 걷기';
    if (t < 10.15) return '“빨간불입니다. 기다려 주세요.”';
    if (t < 13.8) return '“초록불이 켜졌습니다.”';
    if (t < 15.8) return '정류장에 도착했어요.';
    if (t < 17.25) return '탈 버스 번호를 말해요';
    if (t < 18.25) return '“143번이 맞습니까?”';
    if (t < 19.3) return '“맞아요”라고 답해요';
    if (t < 22.05) return '“목표 버스가 접근 중입니다.”';
    if (t < 25.35) return '“도착했습니다.”';
    return '버스에 탑승했어요';
  }
  function draw(c, t, width, height, background = null) {
    t = clamp(t, 0, DURATION);
    c.save(); c.clearRect(0, 0, width, height);
    if (background) { c.fillStyle = background; c.fillRect(0, 0, width, height); }
    const ratio = Math.min(width / 600, height / 455);
    c.translate((width - ratio * 600) / 2, (height - ratio * 455) / 2); c.scale(ratio, ratio);
    let alpha = 1;
    if (t >= 27.35) {
      if (t < 27.65) alpha = 1 - ease(span(t, 27.35, 27.65));
      else { alpha = ease(span(t, 27.65, 28)); t = 0; }
    } else if (t > 6.15 && t < 6.4) alpha = 1 - ease(span(t, 6.15, 6.4));
    else if (t >= 6.4 && t < 6.65) alpha = ease(span(t, 6.4, 6.65));
    else if (t > 13.55 && t < 13.8) alpha = 1 - ease(span(t, 13.55, 13.8));
    else if (t >= 13.8 && t < 14.05) alpha = ease(span(t, 13.8, 14.05));
    c.globalAlpha *= alpha;
    if (t < 6.4) street(c, t);
    else if (t < 13.8) crossing(c, t);
    else station(c, t);
    c.restore();
  }

  let loading;
  function load() {
    if (!loading) loading = Promise.all(['hero', 'passer', 'bus'].map(kind => new Promise((resolve, reject) => {
      const image = new Image(); image.onload = () => resolve([kind, image]); image.onerror = reject; image.src = source[kind].src;
    }))).then(entries => setImages(Object.fromEntries(entries)));
    return loading;
  }
  function mount(canvas, caption, endingCanvas) {
    const ctx = canvas.getContext('2d');
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let time = 0, playing = !motion.matches, ready = false;
    let visible = false, destroyed = false, previous = 0, raf = 0, lastCaption = '';
    function paint() {
      if (destroyed) return;
      if (ready && endingCanvas) poster(endingCanvas);
      const r = canvas.getBoundingClientRect(); if (!r.width || !r.height) return;
      const d = Math.min(window.devicePixelRatio || 1, 2);
      const w = Math.round(r.width * d), h = Math.round(r.height * d);
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      draw(ctx, time, w, h);
      const label = description(time);
      if (caption && label !== lastCaption) {
        caption.textContent = label; lastCaption = label;
        if (caption.parentElement) caption.parentElement.dataset.voice = String(label.startsWith('“'));
      }
    }
    function cancel() { if (raf) cancelAnimationFrame(raf); raf = 0; previous = 0; }
    function run() { if (!destroyed && ready && visible && playing && !raf && !document.hidden) raf = requestAnimationFrame(tick); }
    function tick(now) {
      if (destroyed || !ready || !visible || !playing || document.hidden) { previous = 0; raf = 0; return; }
      if (previous) time = (time + Math.min((now - previous) / 1000, .1)) % DURATION;
      previous = now; paint(); raf = requestAnimationFrame(tick);
    }
    const observer = new ResizeObserver(paint);
    observer.observe(canvas);
    if (endingCanvas) observer.observe(endingCanvas);
    function onVisibility() { cancel(); run(); }
    function onMotion() { playing = !motion.matches; cancel(); paint(); run(); }
    document.addEventListener('visibilitychange', onVisibility);
    motion.addEventListener('change', onMotion);
    load().then(() => { ready = true; paint(); run(); }).catch(() => {
      if (!destroyed && caption) caption.textContent = '일러스트를 불러오지 못했어요.';
    });
    paint();
    return {
      setVisible(value) { visible = value; cancel(); paint(); run(); },
      restart() { time = 0; playing = !motion.matches; cancel(); paint(); run(); },
      destroy() {
        destroyed = true; visible = false; cancel(); observer.disconnect();
        document.removeEventListener('visibilitychange', onVisibility);
        motion.removeEventListener('change', onMotion);
      }
    };
  }
  function poster(canvas) {
    const r = canvas.getBoundingClientRect(); if (!r.width) return;
    const d = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(r.width * d); canvas.height = Math.round(r.height * d);
    draw(canvas.getContext('2d'), 25.35, canvas.width, canvas.height);
  }
  globalThis.GildongmuFilm = { draw, setImages, mount, poster, description, duration: DURATION };
})();
