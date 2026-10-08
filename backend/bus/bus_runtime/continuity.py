"""Short, unambiguous bus-box continuity without using OCR text as identity."""
from __future__ import annotations

from dataclasses import dataclass


def _box(bus):
    if 'bus_box' in bus:
        return tuple(bus['bus_box'])
    return tuple(bus['box'][name] for name in ('x1', 'y1', 'x2', 'y2'))


def _iou(left, right):
    a, b, c, d = left
    x, y, z, w = right
    intersection = max(0, min(c, z) - max(a, x)) * max(0, min(d, w) - max(b, y))
    union = (c - a) * (d - b) + (z - x) * (w - y) - intersection
    return intersection / union if union > 0 else 0.0


@dataclass
class _Track:
    box: tuple
    last: float
    observations: int = 0


class BusTrackContinuity:
    """Recover a missing detector ID after two separate, consistent observations.

    A changed detector ID inherits history only for a unique, recent geometric
    match. Negative IDs belong to provisional tracks; detector IDs stay intact
    unless linked to a previously observed vehicle. Overlapping/ambiguous buses
    never acquire a provisional ID. OCR eligibility is checked independently.
    """
    def __init__(self, *, min_iou=.7, max_gap_s=1.0, max_overlap=.3):
        self.min_iou = min_iou
        self.max_gap_s = max_gap_s
        self.max_overlap = max_overlap
        self.reset()

    def reset(self):
        self._tracks: dict[int, _Track] = {}
        self._aliases: dict[int, int] = {}
        self._next_provisional = -1
        self._last_capture = None

    def update(self, buses: list[dict], timestamp: float):
        if self._last_capture is not None and timestamp <= self._last_capture:
            return
        self._last_capture = timestamp
        expired = {key for key, value in self._tracks.items()
                   if timestamp - value.last > self.max_gap_s}
        for key in expired:
            del self._tracks[key]
        self._aliases = {raw: key for raw, key in self._aliases.items()
                         if key in self._tracks}

        boxes = [_box(bus) for bus in buses]
        raw_ids = [bus['track_id'] for bus in buses]
        ambiguous = [any(i != j and _iou(box, other) > self.max_overlap
                         for j, other in enumerate(boxes))
                     for i, box in enumerate(boxes)]
        assigned = {}
        used = set()
        for i, raw in enumerate(raw_ids):
            key = self._aliases.get(raw) if raw is not None else None
            if key is not None and key not in used:
                assigned[i] = key
                used.add(key)

        candidates = {}
        for i, box in enumerate(boxes):
            if i in assigned:
                continue
            candidates[i] = [] if ambiguous[i] else [
                key for key, value in self._tracks.items()
                if key not in used and 0 < timestamp - value.last <= self.max_gap_s
                and _iou(box, value.box) >= self.min_iou]
        for i, options in candidates.items():
            if len(options) == 1 and sum(options[0] in other for other in candidates.values()) == 1:
                assigned[i] = options[0]
                used.add(options[0])

        for i, bus in enumerate(buses):
            key = assigned.get(i)
            if key is None:
                key = raw_ids[i]
                if key is None or key in used:
                    key = self._next_provisional
                    self._next_provisional -= 1
                used.add(key)
                self._tracks[key] = _Track(boxes[i], timestamp)
            track = self._tracks[key]
            track.observations += 1
            track.last = timestamp
            track.box = boxes[i]
            raw = raw_ids[i]
            if raw is not None:
                self._aliases[raw] = key
            recovered = raw is None and track.observations >= 2 and not ambiguous[i]
            bus['detector_track_id'] = raw
            bus['track_id'] = key if raw is not None or recovered else None
            bus['track_source'] = ('continuity' if raw is not None and key != raw
                                   else 'detector' if raw is not None
                                   else 'provisional' if recovered else 'untracked')
