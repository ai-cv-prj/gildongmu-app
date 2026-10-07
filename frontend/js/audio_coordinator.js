/**
 * file_path: frontend/js/audio_coordinator.js
 *
 * 횡단보도·신호등·보행로·장애물 안내를 한 재생기에서 우선순위대로 관리한다.
 * 지난 장면의 음성은 대기열에 쌓지 않고 유효한 현재 안내만 재생한다.
 */
(() => {
  const PRIORITY = Object.freeze({ emergency: 0, crosswalk: 1, trafficRed: 2,
    walkingSurface: 3, walking: 4, trafficChange: 5, traffic: 6, boarding: 7,
    busOcr: 8, busArrival: 9 });

  // 전체 안내에서 하나뿐인 음성 재생 관리자 생성
  /** 단일 플레이어의 취소와 반복을 안내 우선순위에 맞춰 제어한다. */
  function create({ player, now = () => performance.now(), onDiagnostic = () => {} }) {
    const audio = window.GConfig.get().audio;
    const crosswalkMaxAgeMs = audio.crosswalk_max_age_ms;
    let active = false, current = null, generation = 0;

    // 현재 요청 음성 한 번 재생
    /** 반복 안내는 같은 요청이 여전히 최신일 때 문장 종료 직후 다시 재생한다. */
    function play(request) {
      if (!active || current !== request || now() >= request.validUntil) {
        clear(request.source);
        return false;
      }
      const version = generation;
      const accepted = player.speak(request.text, request.validUntil, {
        dynamic: request.dynamic,
        onStart: () => {
          if (!active || version !== generation || current !== request) return;
          request.started = true;
          request.onStart();
          onDiagnostic({ ...request.metadata, source: request.source, status: "started", at_ms: now() });
        },
        onFailure: () => {
          onDiagnostic({ ...request.metadata, source: request.source, status: "failed", at_ms: now() });
          if (current === request) { current = null; request.onCancel(); }
        },
        onEnd: () => {
          if (!active || version !== generation || current !== request) return;
          if (request.repeat && now() < request.validUntil) play(request);
          else { current = null; request.onComplete(); }
        } });
      if (!accepted) {
        onDiagnostic({ ...request.metadata, source: request.source,
          status: "rejected", at_ms: now() });
        if (current === request) current = null;
      }
      return accepted;
    }

    // 일반 안내 재생 요청
    /** 더 높은 우선순위만 현재 음성을 중단하며 나머지는 대기시키지 않고 폐기한다. */
    function request({ source, priority, text, validUntil, repeat = false, dynamic = false,
      kind = null, metadata = {}, onStart = () => {}, onComplete = () => {}, onCancel = () => {} }) {
      if (!active || !text || !Number.isFinite(validUntil) || now() >= validUntil) {
        onDiagnostic({ ...metadata, source, status: "stale", at_ms: now() });
        return false;
      }
      if (current) {
        if (current.source === source && current.text === text && current.kind === kind) {
          current.validUntil = Math.max(current.validUntil, validUntil);
          current.repeat = repeat;
          current.finishing = false;
          current.metadata = metadata;
          return true;
        }
        const urgentReplacement = priority <= PRIORITY.walking && current.source === source
          && priority === current.priority;
        if (priority > current.priority || (priority === current.priority && !urgentReplacement)) {
          onDiagnostic({ ...metadata, source, status: "priority_rejected", at_ms: now() });
          return false;
        }
        onDiagnostic({ ...current.metadata, source: current.source,
          status: "cancelled", at_ms: now() });
        const previous = current;
        generation++;
        player.cancel();
        previous.onCancel();
      }
      current = { source, priority, text, validUntil, repeat, dynamic, kind, finishing: false,
        metadata, onStart, onComplete, onCancel, started: false };
      return play(current);
    }

    // 현재 문장을 유지한 채 반복만 종료
    /** 복귀한 이탈 안내는 재생 중인 문장을 자르지 않고 다음 반복만 막는다. */
    function finishRepeat(source, kind) {
      if (!current || current.source !== source || current.kind !== kind) return false;
      current.repeat = false;
      current.finishing = true;
      current.validUntil = Math.max(current.validUntil, now() + audio.playback_timeout_ms);
      return true;
    }

    // 특정 안내 종류만 취소
    /** 다른 종류가 재생 중이면 건드리지 않고 요청한 종류의 현재 음성만 중단한다. */
    function clear(source) {
      if (!current || current.source !== source) return;
      onDiagnostic({ ...current.metadata, source: current.source,
        status: "cancelled", at_ms: now() });
      const previous = current;
      generation++;
      current = null;
      player.cancel();
      previous.onCancel();
    }

    // 횡단보도 안전 판정 수신
    /** 진입 전 정렬과 방향이 확정된 이탈만 반복 안내한다. */
    function acceptCrosswalk(event, capturedAt) {
      if (!active || !event || !Number.isFinite(capturedAt)) return;
      const staleAfter = Number.isFinite(event.stale_after_ms)
        ? Math.max(100, event.stale_after_ms) : crosswalkMaxAgeMs;
      const validUntil = capturedAt + staleAfter;
      if (["align_left", "align_right"].includes(event.status)
          && event.repeat && event.voice_text) {
        request({ source: "crosswalk", priority: PRIORITY.crosswalk, text: event.voice_text,
          validUntil, repeat: true, kind: "align" });
        return;
      }
      if (["outside_left", "outside_right"].includes(event.status)
          && event.repeat && event.voice_text) {
        request({ source: "crosswalk", priority: PRIORITY.crosswalk, text: event.voice_text,
          validUntil, repeat: true, kind: "exit" });
        return;
      }
      if (event.status === "uncertain" && event.crossing_active
          && current?.source === "crosswalk") {
        current.validUntil = Math.max(current.validUntil, validUntil);
        return;
      }
      if (finishRepeat("crosswalk", "exit")) return;
      clear("crosswalk");
    }

    // 보행로 이탈 판정 수신
    /** 방향이 확정된 보행로 이탈만 반복하고 복귀 시 현재 문장은 끝까지 재생한다. */
    function acceptWalkingSurface(event, capturedAt) {
      if (!active || !event || !Number.isFinite(capturedAt)) return;
      const staleAfter = Number.isFinite(event.stale_after_ms)
        ? Math.max(100, event.stale_after_ms) : crosswalkMaxAgeMs;
      const validUntil = capturedAt + staleAfter;
      if (["outside_left", "outside_right"].includes(event.status)
          && event.repeat && event.voice_text) {
        request({ source: "walkingSurface", priority: PRIORITY.walkingSurface,
          text: event.voice_text, validUntil, repeat: true, kind: "exit" });
        return;
      }
      finishRepeat("walkingSurface", "exit");
    }

    // 음성 관리자 시작
    /** 새 세션에서 이전 이벤트와 취소 토큰을 초기화한다. */
    function start() {
      stop();
      active = true;
    }

    // 모든 음성 관리자 종료
    /** 세션 종료 시 반복 음성과 남은 플레이어 요청을 즉시 취소한다. */
    function stop() {
      const previous = current;
      if (current) onDiagnostic({ ...current.metadata, source: current.source,
        status: "cancelled", at_ms: now() });
      active = false;
      current = null;
      generation++;
      player.cancel();
      previous?.onCancel();
    }

    // 오래된 이탈 결과 정리
    /** 서버 응답이 끊기면 마지막 이탈 안내가 무한 반복되지 않게 중단한다. */
    function tick() {
      if (current && now() >= current.validUntil && (!current.started || current.repeat)) clear(current.source);
    }

    // 실제 재생 중인 장애물 안내 행동 조회
    /** 장애물 음원의 시작 콜백 후부터 종료·취소 전까지만 행동을 반환한다. */
    function walkingPlaybackAction() {
      if (!current || current.source !== "walking" || !current.started) return null;
      const action = current.metadata?.action;
      return ["left", "right", "stop"].includes(action) ? action : null;
    }

    return { start, stop, request, clear, acceptCrosswalk, acceptWalkingSurface,
      tick, walkingPlaybackAction, PRIORITY };
  }

  window.GAudioCoordinator = { create, PRIORITY };
})();
