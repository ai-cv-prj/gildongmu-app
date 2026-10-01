"""
file_path: src/voice_priority.py

영상 결과의 횡단보도·장애물·신호등 음성을 하나의 우선순위 시간축으로 합친다.
횡단보도 이탈 구간은 문장을 연속 반복하고 복귀 시점에 즉시 자른다.
"""

from src.video_audio import SAMPLE_RATE, decode_clip


TRAFFIC_CHANGE_CLIPS = {"red-changed.mp3", "green-changed.mp3"}
RED_TRAFFIC_CLIPS = {"red.mp3", "red-changed.mp3"}


# 음원 길이 계산
def clip_duration(filename):
    """MP3를 해독해 단일 채널 16 kHz 기준 재생 시간을 초로 반환한다."""
    return len(decode_clip(filename)) / (2 * SAMPLE_RATE)


class CrosswalkVoice:
    """프레임별 이탈 판정을 방향별 연속 구간으로 기록한다."""

    # 횡단보도 음성 구간 기록기 초기화
    def __init__(self):
        """현재 이탈 구간과 완료된 구간을 빈 상태로 준비한다."""
        self.active = None
        self.intervals = []

    # 한 프레임의 이탈 상태 관측
    def observe(self, event, time_s, frame_duration_s):
        """확정 이탈이면 구간을 연장하고 복귀·불확실이면 해당 시각에 닫는다."""
        clip = event.get("voice_clip") if event and event.get("repeat") else None
        if clip:
            if self.active is None or self.active["clip"] != clip:
                self._close(time_s)
                self.active = {"start": time_s, "end": time_s + frame_duration_s, "clip": clip}
            else:
                self.active["end"] = max(self.active["end"], time_s + frame_duration_s)
            return
        self._close(time_s)

    # 현재 이탈 구간 닫기
    def _close(self, time_s):
        """열린 구간을 복귀나 불확실 판정을 처음 받은 현재 시각에 종료한다."""
        if self.active is None:
            return
        end = time_s
        if end > self.active["start"]:
            self.intervals.append((self.active["start"], end, self.active["clip"]))
        self.active = None

    # 영상 종료 시 구간과 반복 이벤트 완성
    def events(self, duration_s):
        """영상 끝에서 열린 구간을 닫고 문장 길이마다 반복할 이벤트를 반환한다."""
        if self.active is not None:
            end = min(duration_s, self.active["end"])
            if end > self.active["start"]:
                self.intervals.append((self.active["start"], end, self.active["clip"]))
            self.active = None
        events = []
        for start, end, clip in self.intervals:
            cursor = start
            step = max(0.01, clip_duration(clip))
            while cursor < end - 1e-9:
                events.append((cursor, clip, 1, "crosswalk"))
                cursor += step
            events.append((end, None, 1, "crosswalk_stop"))
        return events


# 세 안내 종류를 전역 우선순위로 병합
def prioritize_voice_events(walking_events, traffic_events, crosswalk_events):
    """상위 음성이 재생 중인 시점의 하위 이벤트를 폐기해 단일 재생 시간축을 만든다."""
    candidates = [(time_s, clip, 3, "walking") for time_s, clip in walking_events]
    candidates += [
        (time_s, clip, 2 if clip in RED_TRAFFIC_CLIPS
         else 4 if clip in TRAFFIC_CHANGE_CLIPS else 5, "traffic")
        for time_s, clip in traffic_events
    ]
    candidates += list(crosswalk_events)
    candidates.sort(key=lambda item: (item[0], item[2]))
    result = []
    active_end = 0.0
    active_priority = None
    active_source = None
    for time_s, clip, priority, source in candidates:
        if source == "crosswalk_stop":
            if active_source == "crosswalk":
                result.append((time_s, None))
                active_end = time_s
                active_priority = None
                active_source = None
            continue
        if time_s < active_end - 1e-9 and priority >= active_priority:
            continue
        result.append((time_s, clip))
        active_end = time_s + clip_duration(clip)
        active_priority = priority
        active_source = source
    return result
