"""
file_path: src/traffic_voice.py

테스트앱의 신호등 음성 문구와 안정화 규칙을 영상 시간에 적용한다.
"""

from src.video_audio import decode_clip, SAMPLE_RATE


STABLE_FRAMES = 3
STABLE_SECONDS = 0.4
MAX_GAP_SECONDS = 1.0
MISSING_SECONDS = 2.0


class TrafficVoice:
    """선택된 신호의 연속 관측을 확인하고 음성 이벤트를 기록한다."""

    # 영상마다 시작 안내와 관측 이력 초기화
    def __init__(self):
        """시작 안내가 끝난 뒤부터 신호 관측을 받는다."""
        self.events = [(0.0, "startup.mp3")]
        self.ready_at = len(decode_clip("startup.mp3")) / (2 * SAMPLE_RATE)
        self.target = None
        self.color = None
        self.candidate = None
        self.last_valid = None
        self.last_frame = None
        self.last_capture = None
        self.confirmed = False
        self.missing_announced = False
        self.last_announced_target = None
        self.last_announced_color = None
        self.queue_end = self.ready_at

    # 현재 대상의 색상 증거 폐기
    def _reset_evidence(self):
        """연속 관측이 끊기면 전환 판단에 쓰던 색상과 후보를 지운다."""
        self.target = None
        self.color = None
        self.candidate = None

    # 음원 재생 시간을 고려한 순차 예약
    def _announce(self, time_s, filename):
        """앞 신호 안내가 끝난 뒤 새 신호 안내를 예약한다."""
        start = max(time_s, self.queue_end)
        self.events.append((start, filename))
        self.queue_end = start + len(decode_clip(filename)) / (2 * SAMPLE_RATE)

    # 한 프레임의 신호 상태 관측
    def observe(self, result, frame_id, time_s):
        """테스트앱과 같은 3프레임·400ms, 소실·반복 억제 규칙을 적용한다."""
        if time_s < self.ready_at:
            return
        if self.last_valid is not None and time_s - self.last_valid > MAX_GAP_SECONDS:
            self._reset_evidence()
        if (self.confirmed and not self.missing_announced and self.last_valid is not None
                and time_s - self.last_valid >= MISSING_SECONDS):
            self.missing_announced = True
            self._announce(time_s, "missing.mp3")
        continuous = (self.last_frame is None or
                      (frame_id == self.last_frame + 1 and self.last_capture is not None
                       and 0 < time_s - self.last_capture <= MAX_GAP_SECONDS))
        if not continuous:
            self._reset_evidence()
        self.last_frame = frame_id
        self.last_capture = time_s

        index = result.get("selected_detection_index")
        detections = result.get("detections") or []
        selected = (detections[index] if type(index) is int and 0 <= index < len(detections)
                    else None)
        target = selected.get("track_id") if selected else None
        next_color = result.get("signal_state")
        if type(target) is not int or next_color not in ("red", "green"):
            self._reset_evidence()
            return
        self.last_valid = time_s
        if self.target != target:
            self._reset_evidence()
            self.target = target
        if self.candidate is None or self.candidate["color"] != next_color:
            self.candidate = {"color": next_color, "since": time_s, "count": 1}
        else:
            self.candidate["count"] += 1
        if (self.candidate["count"] < STABLE_FRAMES or
                time_s - self.candidate["since"] < STABLE_SECONDS - 1e-9 or self.color == next_color):
            return

        previous = self.color
        self.color = next_color
        first = not self.confirmed
        recovered = self.missing_announced
        self.confirmed = True
        self.missing_announced = False
        if previous is not None and previous != next_color:
            clip = "green-changed.mp3" if next_color == "green" else "red-changed.mp3"
        elif next_color == "green":
            clip = "green-initial-wait.mp3" if first else "green.mp3"
        else:
            clip = "red.mp3"
        if self.last_announced_target == target and self.last_announced_color == next_color and not recovered:
            return
        self.last_announced_target = target
        self.last_announced_color = next_color
        self._announce(time_s, clip)
