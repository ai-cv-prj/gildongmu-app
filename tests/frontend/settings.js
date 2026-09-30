/**
 * file_path: tests/frontend/settings.js
 *
 * 브라우저 회귀 테스트에서 서버와 동일한 YAML 공개 설정을 읽는다.
 */
const { execFileSync } = require("node:child_process");
const { resolve } = require("node:path");

const root = resolve(__dirname, "../..");
module.exports = JSON.parse(execFileSync(resolve(root, ".venv/bin/python"), ["-c",
  "import json; from src.settings import browser_settings, load_app_config, load_audio_settings; " +
  "print(json.dumps(browser_settings(load_app_config(), load_audio_settings())))"],
{ cwd: root, encoding: "utf8" }));
