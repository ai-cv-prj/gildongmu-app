const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const settings = require("./settings");
let utterance, audioInstance, speechCancels = 0, cancelledAudio = 0, started = 0, finished = 0;
class Audio {
  constructor() { audioInstance = this; this.defaultPlaybackRate = 1; this.playbackRate = 1; }
  pause() { cancelledAudio++; }
  removeAttribute() {}
  // 브라우저처럼 load()가 재생 속도를 기본값으로 되돌린다.
  load() { this.playbackRate = this.defaultPlaybackRate; }
  play() { this.onplaying(); }
}
const context = { window: { GConfig: { get: () => settings }, Audio,
  SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
  speechSynthesis: { getVoices: () => [{ lang: "ko-KR" }], speak: value => { utterance = value; value.onstart(); },
    cancel: () => speechCancels++ } }, performance: { now: () => 0 },
  setTimeout: () => 1, clearTimeout() {} };
vm.runInNewContext(fs.readFileSync("frontend/js/tts.js", "utf8"), context);
const player = context.window.GTts.create({ now: () => 0 });
const question = "정류장입니다. 버스를 선택하세요.";
assert.equal(player.speak(question, 1000, { onStart: () => started++, onEnd: () => finished++ }), true);
assert.equal(utterance.lang, "ko-KR");
assert.equal(utterance.text, question);
assert.equal(started, 1);
player.cancel();
assert.equal(speechCancels, 1);
utterance.onend();
assert.equal(finished, 0);
assert.equal(player.speak("등록되지 않은 안내", 1000), false);
assert.equal(started, 1);
assert.equal(player.speak("멈추세요", 1000, { onStart: () => started++ }), true);
assert.equal(started, 2);
assert.equal(audioInstance.playbackRate, 1.5);
    assert.equal(audioInstance.src, "/audio/walking-stop.mp3?v=sesac-212-v3");
player.cancel();
assert.equal(player.speak("혼잡 주의", 1000), true);
assert.equal(audioInstance.src, "/audio/walking-crowded.mp3?v=sesac-212-v3");
player.cancel();
assert.equal(player.speak("전방 장애물", 1000), true);
assert.equal(audioInstance.src, "/audio/walking-obstacle.mp3?v=sesac-212-v3");
player.cancel();
assert.equal(cancelledAudio, 3);
assert.equal(player.setRate(0.75), 0.75);
assert.equal(player.speak("오른쪽 두 걸음", 1000), true);
assert.equal(audioInstance.src, "/audio/walking-move-right-two.mp3?v=sesac-212-v3");
assert.equal(audioInstance.playbackRate, 0.75);
player.cancel();
console.log("tts: pass");
