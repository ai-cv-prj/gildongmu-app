/** Large route keypad, including the preview's Korean/English route prefixes. */
(function (root) {
  "use strict";

  const DIGITS = { 영: 0, 공: 0, 일: 1, 이: 2, 삼: 3, 사: 4, 오: 5, 육: 6, 륙: 6, 칠: 7, 팔: 8, 구: 9 };
  const UNITS = { 십: 10, 백: 100, 천: 1000, 만: 10000 };
  const LETTER_NAMES = { 지: "G", 엠: "M", 엔: "N", 비: "B", 에이: "A", 씨: "C", 디: "D", 이: "E", 에프: "F", 에이치: "H", 아이: "I", 제이: "J", 케이: "K", 엘: "L", 오: "O", 피: "P", 큐: "Q", 알: "R", 에스: "S", 티: "T", 유: "U", 브이: "V", 더블유: "W", 엑스: "X", 와이: "Y", 제트: "Z" };
  const CONSONANTS = [..."ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"];
  const VOWELS = [..."ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"];
  const FINALS = ["", ..."ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"];
  const VOWEL_PAIRS = { "ㅗㅏ": "ㅘ", "ㅗㅐ": "ㅙ", "ㅗㅣ": "ㅚ", "ㅜㅓ": "ㅝ", "ㅜㅔ": "ㅞ", "ㅜㅣ": "ㅟ", "ㅡㅣ": "ㅢ", "ㅏㅣ": "ㅐ", "ㅓㅣ": "ㅔ" };
  const FINAL_PAIRS = { "ㄱㅅ": "ㄳ", "ㄴㅈ": "ㄵ", "ㄴㅎ": "ㄶ", "ㄹㄱ": "ㄺ", "ㄹㅁ": "ㄻ", "ㄹㅂ": "ㄼ", "ㄹㅅ": "ㄽ", "ㄹㅌ": "ㄾ", "ㄹㅍ": "ㄿ", "ㄹㅎ": "ㅀ", "ㅂㅅ": "ㅄ" };

  function numberText(value) {
    const text = value.replace(/\s/g, "");
    if (/^\d+(?:-\d+)?$/.test(text)) return text;
    if (!text || ![...text].every(char => char in DIGITS || char in UNITS)) return null;
    if (![...text].some(char => char in UNITS)) return [...text].map(char => DIGITS[char]).join("");
    let total = 0, section = 0, digit = 0, hasDigit = false, lastUnit = 10000;
    for (const char of text) {
      if (char in DIGITS) {
        if (hasDigit) return null;
        digit = DIGITS[char]; hasDigit = true;
        continue;
      }
      const unit = UNITS[char];
      if (unit === 10000) {
        if (total || lastUnit === 10000 && section === 0 && !hasDigit) return null;
        total = (section + digit || 1) * unit;
        section = 0; digit = 0; hasDigit = false; lastUnit = 10000;
        continue;
      }
      if (unit >= lastUnit || hasDigit && digit === 0) return null;
      section += (hasDigit ? digit : 1) * unit;
      digit = 0; hasDigit = false; lastUnit = unit;
    }
    return String(total + section + digit);
  }

  function normalize(raw) {
    if (typeof raw !== "string" || raw.length > 160) return null;
    let text = raw.normalize("NFKC").trim().replace(/[.!?。！？]+$/g, "").trim();
    for (let index = 0; index < 5; index++) {
      const next = text.replace(/^(?:저는|제가|나는|탈|타려는|일반\s*버스|마을\s*버스|광역\s*버스|버스\s*번호(?:는|가)?|버스(?:는)?|번호(?:는|가)?)\s*/, "").trim();
      if (next === text) break;
      text = next;
    }
    text = text.replace(/(?:버스(?:를|로)?\s*)?(?:타요|타고\s*싶어요|탈게요|찾아\s*주세요|부탁합니다)$/, "").trim();
    text = text.replace(/(?:입니다|이에요|예요|이요|요)$/, "").trim();
    text = text.replace(/\s*버스(?:를|로|는)?$/, "").trim().replace(/\s*번$/, "").trim();
    for (const [name, letter] of Object.entries(LETTER_NAMES).sort((a, b) => b[0].length - a[0].length)) {
      if (text.startsWith(name + " ")) {
        const rest = text.slice(name.length).trim();
        if (numberText(rest) !== null) { text = letter + rest; break; }
      }
    }
    const match = text.match(/^([A-Za-z가-힣]*?)\s*([0-9]+(?:\s*[0-9]+)*(?:\s*-\s*[0-9]+)?|[영공일이삼사오육륙칠팔구십백천만]+(?:\s+[영공일이삼사오육륙칠팔구십백천만]+)*)\s*([A-Za-z]{0,4})$/);
    if (!match) return null;
    let prefix = match[1].toUpperCase();
    if (LETTER_NAMES[prefix]) prefix = LETTER_NAMES[prefix];
    const number = numberText(match[2]);
    if (number === null || number.replace(/\D/g, "").length > 4) return null;
    if (prefix && !/^(?:[A-Z]{1,4}|[가-힣]{1,8})$/.test(prefix)) return null;
    if (/버스|번호|타는|아니|입력|찾아|입니다|주세요|저는|제가|번/.test(prefix)) return null;
    const value = prefix + number + match[3].toUpperCase();
    return value.length <= 20 ? value : null;
  }

  function composeHangul(raw) {
    const chars = [...raw];
    let result = "", index = 0;
    while (index < chars.length) {
      const initial = CONSONANTS.indexOf(chars[index]), medial = VOWELS.indexOf(chars[index + 1]);
      if (initial < 0 || medial < 0) { result += chars[index++]; continue; }
      let vowel = chars[index + 1], next = index + 2, final = 0;
      if (VOWEL_PAIRS[vowel + chars[next]]) { vowel = VOWEL_PAIRS[vowel + chars[next]]; next++; }
      const candidate = FINALS.indexOf(chars[next]);
      if (candidate > 0 && VOWELS.indexOf(chars[next + 1]) < 0) {
        final = candidate; next++;
        const pair = FINAL_PAIRS[FINALS[final] + chars[next]];
        if (pair && VOWELS.indexOf(chars[next + 1]) < 0) { final = FINALS.indexOf(pair); next++; }
      }
      result += String.fromCharCode(44032 + initial * 588 + VOWELS.indexOf(vowel) * 28 + final);
      index = next;
    }
    return result;
  }

  function create({ onChange = () => {}, onSubmit = () => {}, onSpeak = () => {}, onOpenChange = () => {} } = {}) {
    const doc = root.document || document;
    const app = doc.getElementById("app"), layer = doc.getElementById("route-keypad-layer");
    const dialog = doc.getElementById("route-keypad-dialog"), output = doc.getElementById("route-keypad-output");
    const error = doc.getElementById("route-keypad-error"), trigger = doc.getElementById("bus-number");
    const numberKeys = doc.getElementById("number-keys"), letterKeys = doc.getElementById("letter-keys");
    const letterModes = layer.querySelector(".letter-modes");
    const listeners = [], inertStates = new Map();
    let opened = false, destroyed = false, draft = "", input = "";
    let mode = "numbers", group = "consonants", page = 0;

    function listen(element, event, callback) {
      element.addEventListener(event, callback);
      listeners.push(() => element.removeEventListener(event, callback));
    }
    function spokenRoute(value) {
      return value.replace(/[0-9]/g, char => ["공", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"][Number(char)]);
    }
    function displayValue() {
      for (const element of [trigger, output]) {
        const label = element.querySelector("span") || element;
        label.textContent = draft || (element === trigger ? "버스 번호 입력" : "버스 번호");
        element.dataset.empty = String(!draft);
        element.classList.toggle("long", draft.length > 7);
        element.removeAttribute("aria-invalid");
      }
      trigger.value = draft;
      trigger.setAttribute("aria-label", draft ? "버스 번호 " + draft + " 수정" : "버스 번호 입력");
      error.hidden = true;
      error.textContent = "";
    }
    function setValue(value) {
      // A view may mirror onChange back here; keep the uncomposed jamo buffer.
      if (value === draft) { displayValue(); return; }
      draft = typeof value === "string" ? value : "";
      input = draft;
      displayValue();
    }
    function syncDraft() {
      draft = composeHangul(input).toUpperCase();
      displayValue();
      onChange(draft);
    }
    function setError(message) {
      error.textContent = message;
      error.hidden = !message;
      if (message) {
        output.setAttribute("aria-invalid", "true");
        (output.querySelector("span") || output).textContent = message;
      } else displayValue();
    }
    function key(label, attribute, value, word = false) {
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "keypad-key" + (word ? " keypad-key-word" : "");
      button.dataset[attribute] = value;
      button.textContent = label;
      return button;
    }
    function renderLetters() {
      layer.querySelectorAll("[data-letter-group]").forEach(button => {
        button.setAttribute("aria-pressed", String(button.dataset.letterGroup === group));
      });
      const focusedIndex = [...letterKeys.children].indexOf(doc.activeElement);
      const chars = group === "english" ? [..."ABCDEFGHIJKLMNOPQRSTUVWXYZ"] : group === "vowels" ? VOWELS : CONSONANTS;
      page %= Math.ceil(chars.length / 9);
      letterKeys.replaceChildren();
      for (let index = 0; index < 9; index++) {
        const char = chars[page * 9 + index];
        if (char) letterKeys.append(key(char, "letter", char));
        else {
          const spacer = doc.createElement("div");
          spacer.className = "keypad-key-spacer";
          spacer.setAttribute("aria-hidden", "true");
          letterKeys.append(spacer);
        }
      }
      letterKeys.append(key("숫자", "letterAction", "numbers", true), key("다음", "letterAction", "page", true), key("삭제", "letterAction", "delete", true));
      if (focusedIndex >= 0) {
        const replacement = letterKeys.children[focusedIndex];
        (replacement?.tagName === "BUTTON" ? replacement : letterKeys.querySelector("button"))?.focus();
      }
    }
    function renderMode() {
      numberKeys.hidden = mode !== "numbers";
      letterKeys.hidden = mode !== "letters";
      letterModes.hidden = mode !== "letters";
      if (mode === "letters") renderLetters();
      dialog.setAttribute("aria-label", mode === "numbers" ? "버스 번호 숫자 입력 팝업" : "버스 번호 한글 영문 입력 팝업");
    }
    function setMode(next) {
      mode = next;
      renderMode();
      (mode === "numbers" ? numberKeys : letterKeys).querySelector("button")?.focus();
    }
    function open(value = "", nextMode = "numbers") {
      if (destroyed) return;
      setValue(value);
      const letters = input.replace(/[0-9-]/g, "");
      group = /^[A-Z]+$/i.test(letters) ? "english" : "consonants";
      page = 0;
      mode = nextMode === "letters" ? "letters" : "numbers";
      if (!opened) {
        for (const child of app.children) {
          if (child === layer || child.contains(layer)) continue;
          inertStates.set(child, child.inert);
          child.inert = true;
        }
      }
      opened = true;
      layer.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      renderMode();
      onOpenChange(true);
      layer.querySelector('[data-keypad-action="close"]')?.focus();
    }
    function close({ restoreFocus = true } = {}) {
      const wasOpen = opened;
      opened = false;
      layer.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      for (const [element, previous] of inertStates) element.inert = previous;
      inertStates.clear();
      if (wasOpen) {
        onOpenChange(false);
        if (restoreFocus && !trigger.closest("[hidden]")) trigger.focus();
      }
    }
    function deleteLast() {
      input = [...input].slice(0, -1).join("");
      syncDraft();
      onSpeak(draft ? spokenRoute(draft) : "입력 없음");
    }
    function inputDigit(value) {
      if (input.replace(/\D/g, "").length >= 4) { onSpeak("숫자 네 자리까지"); return; }
      input += value;
      syncDraft();
      onSpeak(spokenRoute(value));
    }
    function inputLetter(value) {
      // English letters follow entry order; Korean route names remain prefixes.
      const digitIndex = input.search(/[0-9]/);
      const next = /^[A-Z]$/i.test(value) || digitIndex < 0
        ? input + value
        : input.slice(0, digitIndex) + value + input.slice(digitIndex);
      if (composeHangul(next).replace(/[0-9-]/g, "").length > 8) { onSpeak("입력 길이 초과"); return; }
      input = next;
      syncDraft();
      onSpeak(value);
    }
    function submit() {
      const parsed = normalize(draft);
      if (!parsed) { setError("번호 다시 입력"); onSpeak("번호 다시 입력"); return; }
      setValue(parsed);
      onChange(parsed);
      onSubmit(parsed);
    }
    listen(layer, "click", event => {
      const button = event.target.closest("button");
      if (!opened || !button || !layer.contains(button)) return;
      if (button.dataset.keypadAction === "close") { close(); return; }
      if (button.dataset.keypadAction === "search") { submit(); return; }
      if (button.dataset.key !== undefined && mode === "numbers") {
        const value = button.dataset.key;
        if (value === "letters") setMode("letters");
        else if (value === "delete") deleteLast();
        else if (/^[0-9]$/.test(value)) inputDigit(value);
        return;
      }
      if (button.dataset.letterGroup && mode === "letters") {
        group = button.dataset.letterGroup;
        if (!["consonants", "vowels", "english"].includes(group)) group = "consonants";
        page = 0;
        renderLetters();
        onSpeak(button.textContent);
        return;
      }
      if (mode !== "letters") return;
      if (button.dataset.letter) inputLetter(button.dataset.letter);
      else if (button.dataset.letterAction === "delete") {
        // The letter keypad removes the last letter while retaining the number.
        input = input.replace(/[^0-9-]([0-9-]*)$/, "$1");
        syncDraft(); onSpeak(draft ? spokenRoute(draft) : "입력 없음");
      } else if (button.dataset.letterAction === "page") {
        page++; renderLetters(); onSpeak("다음");
      } else if (button.dataset.letterAction === "numbers") { setMode("numbers"); onSpeak("숫자"); }
    });
    listen(doc, "keydown", event => {
      if (!opened || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (event.key === "Tab") {
        const buttons = [...dialog.querySelectorAll("button")].filter(button => !button.disabled && !button.closest("[hidden]"));
        const first = buttons[0], last = buttons.at(-1);
        if (!first) return;
        if (event.shiftKey && (doc.activeElement === first || !dialog.contains(doc.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (doc.activeElement === last || !dialog.contains(doc.activeElement))) { event.preventDefault(); first.focus(); }
        return;
      }
      // Enter on a focused button must perform that button's native action.
      if (event.key === "Enter") {
        if (doc.activeElement?.closest("button")) return;
        event.preventDefault(); submit();
      }
      else if (event.key === "Backspace") { event.preventDefault(); deleteLast(); }
      else if (/^[0-9]$/.test(event.key)) { event.preventDefault(); inputDigit(event.key); }
      else if (event.key === "-") {
        event.preventDefault();
        if (/[0-9]$/.test(input) && !input.includes("-") && input.replace(/\D/g, "").length < 4) {
          input += "-"; syncDraft(); onSpeak("하이픈");
        }
      } else if (/^[a-z]$/i.test(event.key)) { event.preventDefault(); inputLetter(event.key.toUpperCase()); }
    });

    return { open, close, setValue, setError, isOpen: () => opened, destroy() {
      close({ restoreFocus: false });
      listeners.forEach(remove => remove());
      destroyed = true;
    } };
  }

  const api = { normalize, composeHangul, create };
  root.GRouteKeypad = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
