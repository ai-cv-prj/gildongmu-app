"""
file_path: src/traffic_voice.py

테스트앱의 신호등 음성 문구와 안정화 규칙을 영상 시간에 적용한다.
"""

from src.settings import load_audio_settings


AUDIO_SETTINGS = load_audio_settings()
STABLE_FRAMES = AUDIO_SETTINGS["guidance"]["stable_frames"]
STABLE_WINDOW_FRAMES = AUDIO_SETTINGS["guidance"]["stable_window_frames"]
STABLE_SECONDS = AUDIO_SETTINGS["guidance"]["stable_ms"] / 1000
MAX_GAP_SECONDS = AUDIO_SETTINGS["video_max_gap_ms"] / 1000
MISSING_SECONDS = AUDIO_SETTINGS["guidance"]["missing_ms"] / 1000
REPEAT_SECONDS = AUDIO_SETTINGS["guidance"]["traffic_repeat_ms"] / 1000
CROSSING_HOLD_SECONDS = AUDIO_SETTINGS["guidance"]["traffic_crossing_hold_ms"] / 1000
COLOR_MEMORY_SECONDS = AUDIO_SETTINGS["guidance"]["traffic_color_memory_ms"] / 1000
# 전환 안내 후 같은 색상을 다시 읽을 때는 현재 색상만 안내한다.
REPEAT_CLIPS = {"green-changed.mp3": "green.mp3", "red-changed.mp3": "red.mp3"}


class TrafficVoice:
    """선택된 신호의 연속 관측을 확인하고 음성 이벤트를 기록한다."""

    # 영상마다 신호 관측 이력 초기화
    def __init__(self):
        """빈 음성 이벤트와 신호 관측 상태를 준비한다."""
        self.events = []
        self.target = None
        self.color = None
        # 현재 대상의 최근 색상 관측이며, 인식불가 프레임은 넣지 않는다.
        self.samples = []
        # 대상 추적이 잠깐 끊겨도 직전에 확정한 대상·색상을 기억해 전환과 다른 신호의 초록을 구분한다.
        self.remembered = None
        self.last_valid = None
        self.last_frame = None
        self.last_capture = None
        self.confirmed = False
        self.missing_announced = False
        self.last_announced_target = None
        self.last_announced_color = None
        self.last_announced_time = None
        self.repeat_clip = None
        self.voice_allowed = False
        # 횡단 중 파란 ROI의 횡단보도 근거가 처음 끊긴 시각이다. 다시 허용되면 지운다.
        self.gate_lost_time = None

    # 횡단보도 근거 소실 시 신호 음성과 관측 이력 해제
    def _suspend(self, time_s):
        """재생 중인 신호만 중단하고 재진입 시 새 안정화 과정을 거치게 한다."""
        if self.voice_allowed:
            self.events.append((time_s, None))
        self.voice_allowed = False
        self._reset_evidence()
        self.last_valid = self.last_frame = self.last_capture = None
        self.confirmed = self.missing_announced = False
        self.last_announced_target = self.last_announced_color = None
        self.last_announced_time = self.repeat_clip = None
        self.remembered = None

    # 횡단 중 짧은 횡단보도 근거 소실 시 신호 음성만 중단
    def _hold(self, time_s):
        """재생 중인 신호를 멈추되 확인한 신호 기억은 유지해 재확인 시 대기 안내를 반복하지 않는다."""
        if self.voice_allowed:
            self.events.append((time_s, None))
        self.voice_allowed = False
        self._reset_evidence()
        # 끊긴 동안은 소실 시간에 포함하지 않는다.
        self.last_valid = time_s
        self.last_frame = self.last_capture = None

    # 현재 대상의 색상 증거 폐기
    def _reset_evidence(self):
        """연속 관측이 끊기면 전환 판단에 쓰던 색상과 후보를 지운다."""
        self.target = None
        self.color = None
        self.samples = []

    # 전역 음성 우선순위에 사용할 신호 이벤트 기록
    def _announce(self, time_s, filename):
        """관측 시각에 이벤트를 기록해 전역 관리자가 중단과 폐기를 결정하게 한다."""
        self.events.append((time_s, filename))

    # 같은 대상·색상의 주기 재안내
    def _repeat(self, time_s):
        """마지막 신호 안내 후 설정 시간이 지나면 같은 문구를 낮은 우선순위로 기록한다."""
        if (self.repeat_clip is None or self.last_announced_target != self.target
                or self.last_announced_color != self.color
                or time_s - self.last_announced_time < REPEAT_SECONDS - 1e-9):
            return
        self.last_announced_time = time_s
        self.events.append((time_s, self.repeat_clip, "repeat"))

    # 한 프레임의 신호 상태 관측
    def observe(self, result, frame_id, time_s, crossing_active=False):
        """테스트앱과 같은 3프레임·400ms, 소실·반복 억제 규칙을 적용한다."""
        if (result.get("voice_gate") or {}).get("allowed") is not True:
            if self.gate_lost_time is None:
                self.gate_lost_time = time_s
            # 건너도 되는 초록불을 안내하고 건너는 중일 때만 유지한다.
            # 빨간불이나 대기 안내 뒤에는 기존처럼 새로 확인해 대기 안내를 이어 간다.
            if (crossing_active and self.repeat_clip == "green.mp3"
                    and time_s - self.gate_lost_time < CROSSING_HOLD_SECONDS - 1e-9):
                self._hold(time_s)
            else:
                self._suspend(time_s)
            return
        self.gate_lost_time = None
        self.voice_allowed = True
        if self.last_valid is not None and time_s - self.last_valid > MAX_GAP_SECONDS:
            self._reset_evidence()
        if (self.confirmed and not self.missing_announced and self.last_valid is not None
                and time_s - self.last_valid >= MISSING_SECONDS):
            self.missing_announced = True
            # 소실을 알린 뒤에는 이전 관측을 버리고 다시 확정한 색상을 안내한다.
            self._reset_evidence()
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
        # 인식불가 프레임은 색상 다수결에서 건너뛰고, 긴 공백은 위의 최대 간격 기준으로 정리한다.
        if type(target) is not int or next_color not in ("red", "green"):
            return
        self.last_valid = time_s
        if self.target != target:
            self._reset_evidence()
            self.target = target
        # 최근 관측 중 같은 색이 기준 개수 이상이면 순간 오인식이 섞여도 색상을 확정한다.
        self.samples.append((next_color, time_s))
        del self.samples[:-STABLE_WINDOW_FRAMES]
        votes = [at for color, at in self.samples if color == next_color]
        if len(votes) < STABLE_FRAMES or time_s - votes[0] < STABLE_SECONDS - 1e-9:
            return
        memory = (self.remembered if self.remembered is not None
                  and time_s - self.remembered[2] <= COLOR_MEMORY_SECONDS + 1e-9 else None)
        self.remembered = (target, next_color, time_s)
        if self.color == next_color:
            self._repeat(time_s)
            return

        # 같은 대상이 짧게 끊긴 뒤 다른 색으로 확인되면 전환으로 안내한다.
        previous = self.color
        if previous is None and memory is not None and memory[0] == target:
            previous = memory[1]
        # 빨간불을 보던 중 다른 신호등의 초록을 잡으면 바뀌는 순간을 보지 못한 초록으로 본다.
        unverified_green = (next_color == "green" and memory is not None
                            and memory[0] != target and memory[1] == "red")
        self.color = next_color
        first = not self.confirmed
        recovered = self.missing_announced
        self.confirmed = True
        self.missing_announced = False
        if previous is not None and previous != next_color:
            clip = "green-changed.mp3" if next_color == "green" else "red-changed.mp3"
        elif next_color == "green":
            clip = "green-initial-wait.mp3" if first or unverified_green else "green.mp3"
        else:
            clip = "red.mp3"
        if self.last_announced_target == target and self.last_announced_color == next_color and not recovered:
            return
        self.last_announced_target = target
        self.last_announced_color = next_color
        self.last_announced_time = time_s
        self.repeat_clip = REPEAT_CLIPS.get(clip, clip)
        self._announce(time_s, clip)
