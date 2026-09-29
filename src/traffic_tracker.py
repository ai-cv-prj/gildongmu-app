"""YOLO 검출에 세션별 BoT-SORT ID를 연결한다.

검증 환경: ultralytics 8.4.150, lap 0.5.13. ReID 모델은 사용하지 않는다.
현재 검출만 반환하고 짧은 가림의 추적 정보는 최대 3초 보관한다.
"""
from types import SimpleNamespace

import numpy as np

RECOVERY_CONFIDENCE = 0.1
LOST_MAX_AGE_MS = 3000
RECOVERY_MIN_IOU = 0.5
RECOVERY_MATCH_MARGIN = 0.1


class SignalTracker:
    def __init__(self):
        # 모델이 필요한 신호등 모드에서만 추적 의존성을 불러온다.
        import lap  # noqa: F401; 실행 중 자동 설치 대신 누락 의존성을 명확히 알린다.
        from ultralytics.trackers.bot_sort import BOTSORT
        from ultralytics.trackers.basetrack import TrackState

        self.next_id = 1
        self.previous_context = None
        self.previous_shape = None
        self.last_seen = {}
        owner = self

        class SessionBOTSORT(BOTSORT):
            @staticmethod
            def reset_id():
                # 다른 세션의 전역 ID 카운터를 초기화하지 않는다.
                pass

            def init_track(self, results, img=None):
                tracks = super().init_track(results, img)
                for track in tracks:
                    track.next_id = owner._allocate_id
                return tracks

            def get_dists(self, tracks, detections):
                distances = super().get_dists(tracks, detections)
                # 소실 ID는 이동 보정·예측 위치와 충분히 겹치고 양방향으로
                # 후보가 명확할 때만 복원한다. 비슷한 후보끼리 ID를 빼앗지 않는다.
                original = distances.copy()
                for i, track in enumerate(tracks):
                    if track.state != TrackState.Lost or not len(detections):
                        continue
                    distances[i, :] = 1.0
                    j = int(original[i].argmin())
                    cost = original[i, j]
                    if cost > 1 - RECOVERY_MIN_IOU:
                        continue
                    row_others = np.delete(original[i], j)
                    column_others = np.delete(original[:, j], i)
                    if (np.any(row_others < cost + RECOVERY_MATCH_MARGIN)
                            or np.any(column_others < cost + RECOVERY_MATCH_MARGIN)):
                        continue
                    distances[i, j] = cost
                return distances

        # 매칭 비용에 검출 신뢰도를 다시 곱하지 않아 낮은 설정에서도 같은 객체를 유지한다.
        self.tracker = SessionBOTSORT(SimpleNamespace(
            tracker_type="botsort", track_high_thresh=0.25, track_low_thresh=0.1,
            new_track_thresh=0.25, track_buffer=30, match_thresh=0.8,
            fuse_score=False, gmc_method="sparseOptFlow", with_reid=False,
            proximity_thresh=0.5, appearance_thresh=0.8, model="auto",
        ))

    def _allocate_id(self):
        result = self.next_id
        self.next_id += 1
        return result

    def update(self, frame, signals, context):
        from ultralytics.engine.results import Boxes

        previous = self.previous_context
        continuous = previous is None or (
            context.frame_id == previous.frame_id + 1
            and 0 <= context.captured_at_ms - previous.captured_at_ms <= 1000
            and frame.shape[:2] == self.previous_shape
        )
        if not continuous:
            self.tracker.reset()
            self.last_seen.clear()
        self.previous_context = context
        self.previous_shape = frame.shape[:2]
        # 처리 FPS와 관계없이 촬영 시각 기준으로 소실 ID를 만료한다.
        for name in ("tracked_stracks", "lost_stracks"):
            retained = []
            for track in getattr(self.tracker, name):
                seen = self.last_seen.get(track.track_id)
                if seen is not None and context.captured_at_ms - seen <= LOST_MAX_AGE_MS:
                    retained.append(track)
                else:
                    track.mark_removed()
            setattr(self.tracker, name, retained)
        # 신규 객체는 사용자 검출 기준을 통과해야 한다. 그보다 약한 검출은
        # BoT-SORT의 2차 매칭에서 이미 추적 중인 객체를 이어 잡는 데만 쓴다.
        threshold = context.confidence
        self.tracker.args.track_high_thresh = threshold
        self.tracker.args.new_track_thresh = threshold
        self.tracker.args.track_low_thresh = min(RECOVERY_CONFIDENCE, threshold)
        data = np.asarray([
            [*signal["xyxy"], signal["confidence"], signal["class_id"]] for signal in signals
        ], dtype=np.float32).reshape(-1, 6)
        self.tracker.update(Boxes(data, frame.shape[:2]), frame)
        for signal in signals:
            signal["track_id"] = None
        # 아직 연속 확인되지 않은 추적도 현재 실제 검출에 연결된 ID는 사용한다.
        # 대상·색상 연속 확인은 별도로 수행하며 칼만 예측 박스를 출력하지 않는다.
        for track in self.tracker.tracked_stracks:
            index = int(track.idx)
            if track.frame_id == self.tracker.frame_id and 0 <= index < len(signals):
                signals[index]["track_id"] = int(track.track_id)
                self.last_seen[int(track.track_id)] = context.captured_at_ms
        live_ids = {track.track_id for track in
                    self.tracker.tracked_stracks + self.tracker.lost_stracks}
        self.last_seen = {key: value for key, value in self.last_seen.items() if key in live_ids}
        self.tracker.removed_stracks.clear()
