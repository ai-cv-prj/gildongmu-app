"""
file_path: src/camera_view.py

Conservative, image-only camera-view quality gate for risk presentation.

A low-detail frame alone is not enough to invalidate a forward view. Ground-like
appearance requires either an implausibly large detector box or sustained loss
of walkable pixels at the bottom of the image.
"""
import cv2
import numpy as np


class CameraViewGuard:
    # 촬영 상태 판정기 초기화
    def __init__(self, cfg):
        """위험 설정을 받아 촬영 상태 판정기를 초기화한다."""
        self.cfg = cfg
        self.reset()

    # 촬영 상태 기록 초기화
    def reset(self):
        """이전 촬영 상태와 복구 대기 시간을 지운다."""
        self.status = "clear"
        self.reason = None
        self.starts = {}
        self.recovery_since = None

    # 상태 후보 지속 시간 계산
    def _duration(self, key, present, timestamp):
        """상태 후보가 이어진 시간을 초 단위로 반환한다."""
        if not present:
            self.starts.pop(key, None)
            return 0.0
        self.starts.setdefault(key, timestamp)
        return max(0.0, timestamp - self.starts[key])

    @staticmethod
    # 화면 대부분을 가리는 검출 박스 확인
    def _large_clipped_box(detections, shape):
        """프레임을 크게 가리는 검출 박스가 있는지 확인한다."""
        h, w = shape[:2]
        for detection in detections:
            box = detection.get("xyxy")
            if box is None or len(box) != 4 or not np.isfinite(box).all():
                continue
            x1, y1, x2, y2 = box
            width = max(0, min(w, x2) - max(0, x1))
            height = max(0, min(h, y2) - max(0, y1))
            edges = sum((x1 <= .03*w, y1 <= .03*h, x2 >= .97*w, y2 >= .97*h))
            if width*height/(w*h) >= .80 and edges >= 2:
                return True
        return False

    # 촬영 상태 갱신
    def update(self, frame, detections, class_map, label_ids, timestamp, camera_stable):
        """원본 프레임과 보행 마스크로 촬영 상태를 갱신한다."""
        h, w = frame.shape[:2]
        # Tiny frames cannot support the calibrated appearance statistics.
        if not self.cfg["camera_view_guard_enabled"] or min(h, w) < 160:
            return {"status": "clear", "reason": None, "changed": False,
                    "message": "", "evidence": {}}
        small = cv2.resize(frame, (180, 320))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        saturation = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
        luma = float(gray.mean())
        luma_std = float(gray.std())
        top, bottom = small[:100], small[220:]
        top_std = float(cv2.cvtColor(top, cv2.COLOR_BGR2GRAY).std())
        a = cv2.calcHist([top], [0, 1, 2], None, [8, 8, 8],
                         [0, 256, 0, 256, 0, 256])
        b = cv2.calcHist([bottom], [0, 1, 2], None, [8, 8, 8],
                         [0, 256, 0, 256, 0, 256])
        cv2.normalize(a, a)
        cv2.normalize(b, b)
        top_bottom_distance = float(cv2.compareHist(
            a, b, cv2.HISTCMP_BHATTACHARYYA))
        ground_like = (h > w and saturation < 40 and top_std < 55
                       and top_bottom_distance < .62)
        bottom_walkable = None
        if (class_map is not None and label_ids is not None
                and class_map.shape == (h, w)
                and "walkable" in label_ids and "crosswalk" in label_ids):
            bottom = class_map[round(h*2/3):]
            bottom_walkable = float(np.isin(
                bottom, [label_ids["walkable"], label_ids["crosswalk"]]).mean())
        large_box = self._large_clipped_box(detections, frame.shape)
        ground_duration = self._duration("ground", ground_like, timestamp)
        opaque = ((luma < 22 or luma > 245) and luma_std < 24) or luma_std < 7
        opaque_duration = self._duration("opaque", opaque, timestamp)
        laplacian = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        edge_fraction = float((cv2.Canny(gray, 60, 150) > 0).mean())
        blurred = laplacian < 70 and edge_fraction < .02
        blur_duration = self._duration("blur", blurred, timestamp)
        motion_duration = self._duration("motion", not camera_stable, timestamp)

        failure = None
        if opaque and opaque_duration >= .15:
            failure = "obscured_view"
        elif blurred and blur_duration >= .25:
            failure = "unreadable_view"
        elif (ground_like and ground_duration + 1e-9 >= .08
              and large_box):
            failure = "ground_dominated_view"
        elif (ground_like and ground_duration >= .80
              and bottom_walkable is not None and bottom_walkable < .20):
            failure = "ground_dominated_view"

        previous = self.status
        if failure:
            self.status, self.reason = "unavailable", failure
            self.recovery_since = None
        elif self.status == "unavailable":
            # A momentary unclassified frame is not proof that the view recovered.
            clean = not (ground_like or opaque or blurred) and camera_stable
            if clean:
                if self.recovery_since is None:
                    self.recovery_since = timestamp
                if timestamp - self.recovery_since + 1e-9 >= .35:
                    self.status, self.reason = "clear", None
            else:
                self.recovery_since = None
        elif not camera_stable and motion_duration >= .25:
            self.status, self.reason = "uncertain", "unstable_camera_motion"
            self.recovery_since = None
        elif self.status == "uncertain":
            if camera_stable:
                if self.recovery_since is None:
                    self.recovery_since = timestamp
                if timestamp - self.recovery_since + 1e-9 >= .35:
                    self.status, self.reason = "clear", None
            else:
                self.recovery_since = None

        message = ("CAUTION | camera view unavailable; point forward"
                   if self.status == "unavailable" else
                   "CAUTION | steady the camera" if self.status == "uncertain"
                   else "")
        return {"status": self.status, "reason": self.reason,
                "changed": previous != self.status, "message": message,
                "evidence": {
                    "saturation": round(saturation, 2),
                    "top_luma_std": round(top_std, 2),
                    "top_bottom_distance": round(top_bottom_distance, 3),
                    "bottom_walkable_fraction": (None if bottom_walkable is None
                                                 else round(bottom_walkable, 3)),
                    "large_clipped_box": large_box,
                    "luma": round(luma, 2),
                    "laplacian": round(laplacian, 2),
                    "edge_fraction": round(edge_fraction, 3),
                }}
