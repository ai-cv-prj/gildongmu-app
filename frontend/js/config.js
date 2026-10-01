/**
 * file_path: frontend/js/config.js
 *
 * 서버 YAML에서 공개한 브라우저 설정을 화면 시작 전에 한 번 읽는다.
 */
window.GConfig = (() => {
  let settings = null;

  // 서버의 공개 설정 준비
  /** 설정 조회 실패 시 카메라를 시작하지 않고 오류를 호출자에게 전달한다. */
  async function load() {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (!response.ok) throw new Error(`설정을 불러올 수 없습니다 (${response.status}).`);
    const value = await response.json();
    if (!value.camera || !value.recording || !value.audio) {
      throw new Error("서버 설정이 올바르지 않습니다.");
    }
    settings = value;
    return settings;
  }

  // 준비된 설정 조회
  /** 설정을 읽기 전에 기능을 실행하면 명확한 초기화 오류를 알린다. */
  function get() {
    if (!settings) throw new Error("서버 설정이 아직 준비되지 않았습니다.");
    return settings;
  }

  return { load, get };
})();
