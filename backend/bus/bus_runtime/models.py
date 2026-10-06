"""YOLO11n/ByteTrack bus boxes and the fixed B route-display model.

This follows the B operating evaluation in
scripts/experiments/bus_roi_finetune_20260930_run01/run_videos.py.
The CRAFT fallback was opt-in there and remains disabled here.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def clip_box(box, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1 = np.floor(box[:2]).astype(int)
    x2, y2 = np.ceil(box[2:]).astype(int)
    return tuple(int(v) for v in (max(0, min(width, x1)), max(0, min(height, y1)),
                                  max(0, min(width, x2)), max(0, min(height, y2))))


class RouteDetector:
    route_display_only = True

    def __init__(self, route_weights: Path, bus_weights: Path, device: str, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault('YOLO_CONFIG_DIR', str(cache_dir))
        from ultralytics import YOLO
        self.device = device
        self.yolo = YOLO(str(bus_weights))
        self.bus_ids = [i for i, name in self.yolo.names.items() if name == 'bus']
        if not self.bus_ids:
            raise ValueError('bus detector does not define a bus class')
        self.route = YOLO(str(route_weights))
        # Match the warm-up and inference dimensions of the evaluated B path.
        self.yolo.predict(np.zeros((640, 640, 3), np.uint8), device=device, verbose=False)
        self.route.predict(np.zeros((960, 960, 3), np.uint8), device=device,
                           imgsz=960, rect=False, conf=.25, iou=.5, verbose=False)
        self.sync()

    def sync(self) -> None:
        if self.device.startswith('cuda'):
            import torch
            torch.cuda.synchronize()

    def reset(self) -> None:
        predictor = self.yolo.predictor
        if predictor is not None and hasattr(predictor, 'trackers'):
            for tracker in predictor.trackers:
                tracker.reset()

    def buses(self, rgb: np.ndarray) -> list[dict]:
        result = self.yolo.track(rgb[:, :, ::-1].copy(), persist=True,
                                 tracker='bytetrack.yaml', classes=self.bus_ids,
                                 conf=.25, imgsz=640, device=self.device, verbose=False)[0]
        height, width = rgb.shape[:2]
        buses = []
        for box in result.boxes:
            bounds = clip_box(box.xyxy[0].cpu().numpy(), width, height)
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            buses.append({'track_id': int(box.id.item()) if box.id is not None else None,
                          'bus_box': bounds, 'bus_score': float(box.conf.item())})
        return buses

    def text_regions(self, rgb: np.ndarray, bus_box: tuple[int, int, int, int]):
        x, y, z, bottom = bus_box
        roi = rgb[y:bottom, x:z]
        if roi.size == 0:
            return bus_box, []
        result = self.route.predict(roi[:, :, ::-1].copy(), device=self.device,
                                    imgsz=960, rect=False, conf=.25, iou=.5, verbose=False)[0]
        boxes = []
        for bounds in result.boxes.xyxy.cpu().tolist():
            a, b, c, d = clip_box(bounds, z-x, bottom-y)
            if c > a and d > b:
                boxes.append((a+x, b+y, c+x, d+y))
        return bus_box, boxes

    def regions_in_box(self, rgb: np.ndarray, search_box, canvas_size=None):
        # B searches the complete bus once. There is no second CRAFT pass.
        return []
