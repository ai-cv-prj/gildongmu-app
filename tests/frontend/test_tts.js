const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const settings = require("./settings");
let utterance, speechCancels = 0, cancelledAudio = 0, started = 0, finished = 0;
class Audio {
  pause() { cancelledAudio++; }
  removeAttribute() {}
  load() {}
  play() { this.onplaying(); }
}
const context = { window: { GConfig: { get: () => settings }, Audio,
  SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
  speechSynthesis: { getVoices: () => [{ lang: "ko-KR" }], speak: value => { utterance = value; value.onstart(); },
    cancel: () => speechCancels++ } }, performance: { now: () => 0 },
  setTimeout: () => 1, clearTimeout() {} };
vm.runInNewContext(fs.readFileSync("frontend/js/tts.js", "utf8"), context);
const player = context.window.GTts.create({ now: () => 0 });
const question = "정류장 근처입니다. 탑승할 버스 번호를 입력해 주세요. 탑승하지 않으면 취소를 누르세요.";
assert.equal(player.speak(question, 1000, { onStart: () => started++, onEnd: () => finished++ }), true);
assert.equal(utterance.lang, "ko-KR");
assert.equal(started, 1);
player.cancel();
assert.equal(speechCancels, 1);
utterance.onend();
assert.equal(finished, 0);
assert.equal(player.speak("멈추세요.", 1000, { onStart: () => started++ }), true);
assert.equal(started, 2);
player.cancel();
assert.equal(cancelledAudio, 1);
console.log("tts: pass");
