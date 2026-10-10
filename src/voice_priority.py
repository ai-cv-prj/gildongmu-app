"""
file_path: src/voice_priority.py

영상 결과의 횡단보도·신호등·보행로·장애물 음성을 한 시간축으로 합친다.
이탈 구간은 문장을 반복하고 복귀하면 재생 중인 문장은 유지한 채 새 반복만 멈춘다.
"""

import math
from functools import lru_cache

from src.video_audio import SAMPLE_RATE, decode_clip


TRAFFIC_CHANGE_CLIPS = {"red-changed.mp3", "green-changed.mp3"}
RED_TRAFFIC_CLIPS = {"red.mp3", "red-changed.mp3"}
EMERGENCY_WALKING_CLIPS = {"walking-stop.mp3"}


# 음원 길이 계산
@lru_cache(maxsize=None)
def clip_duration(filename):
    """MP3를 해독해 단일 채널 16 kHz 기준 재생 시간을 초로 반환한다."""
    return len(decode_clip(filename)) / (2 * SAMPLE_RATE)


class CrosswalkVoice:
    """프레임별 반복 안내를 방향별 연속 구간으로 기록한다."""

    # 횡단보도 음성 구간 기록기 초기화
    def __init__(self, source="crosswalk", priority=1):
        """음성 출처와 우선순위를 저장하고 반복 구간을 빈 상태로 준비한다."""
        self.source = source
        self.priority = priority
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
        """같은 방향의 짧은 복귀를 이어 문장이 끝나는 경계마다 반복한다."""
        if self.active is not None:
            end = min(duration_s, self.active["end"])
            if end > self.active["start"]:
                self.intervals.append((self.active["start"], end, self.active["clip"]))
            self.active = None
        merged = []
        for start, end, clip in self.intervals:
            step = max(0.01, clip_duration(clip))
            if merged and merged[-1][2] == clip:
                previous_start, previous_end, _ = merged[-1]
                repeats = max(1, math.ceil((previous_end - previous_start) / step))
                audible_end = previous_start + repeats * step
                if start < audible_end - 1e-9:
                    merged[-1] = (previous_start, max(previous_end, end), clip)
                    continue
            merged.append((start, end, clip))
        events = []
        for start, end, clip in merged:
            cursor = start
            step = max(0.01, clip_duration(clip))
            while cursor < end - 1e-9:
                events.append((cursor, clip, self.priority, self.source))
                cursor += step
        return events


# 저장 영상의 모든 안내를 전역 우선순위로 병합
def prioritize_voice_events(walking_events, traffic_events, crosswalk_events,
                            walking_surface_events=()):
    """상위 음성이 재생 중인 시점의 하위 이벤트를 폐기해 단일 재생 시간축을 만든다."""
    candidates = [(time_s, clip, 0 if clip in EMERGENCY_WALKING_CLIPS else 4,
                   "walking" if clip else "walking_stop")
                  for time_s, clip in walking_events]
    # 같은 색상 재안내는 보행 안내를 끊지 않도록 가장 낮은 신호 우선순위를 쓴다.
    candidates += [
        (time_s, clip, 6 if "repeat" in flags else 2 if clip in RED_TRAFFIC_CLIPS
         else 5 if clip in TRAFFIC_CHANGE_CLIPS else 6, "traffic" if clip else "traffic_stop")
        for time_s, clip, *flags in traffic_events
    ]
    candidates += list(crosswalk_events)
    candidates += list(walking_surface_events)
    candidates.sort(key=lambda item: (item[0], item[2]))
    result = []
    active_end = 0.0
    active_priority = None
    active_source = None
    active_clip = None
    for time_s, clip, priority, source in candidates:
        if source == "traffic_stop":
            if active_source == "traffic":
                result.append((time_s, None))
                active_end = time_s
                active_priority = None
                active_source = None
                active_clip = None
            continue
        if source == "walking_stop":
            if active_source == "walking":
                result.append((time_s, None))
                active_end = time_s
                active_priority = None
                active_source = None
                active_clip = None
            continue
        if source == "crosswalk_stop":
            if active_source == "crosswalk":
                result.append((time_s, None))
                active_end = time_s
                active_priority = None
                active_source = None
                active_clip = None
            continue
        same_source_urgent_change = (source == active_source
                                     and priority == active_priority
                                     and priority <= 4
                                     and clip != active_clip)
        if (time_s < active_end - 1e-9 and priority >= active_priority
                and not same_source_urgent_change):
            continue
        result.append((time_s, clip))
        active_end = time_s + clip_duration(clip)
        active_priority = priority
        active_source = source
        active_clip = clip
    return result
