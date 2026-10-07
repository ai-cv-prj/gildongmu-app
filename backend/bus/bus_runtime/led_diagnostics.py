"""Diagnostic-only LED scan-gap measurement. It never changes OCR or matching.

With a short exposure a multiplexed LED sign records only a few lit rows per
frame. Within the vertical span of lit pixels, a clean sign has nearly every
row lit; a broken frame keeps only a few thin lines. The threshold below is
from two field clips only and is recorded for later validation.
"""
from __future__ import annotations

import numpy as np

DIAGNOSTIC_VERSION = 1
SUSPECT_BELOW = .8  # unvalidated: clean 0.90-0.98, broken 0.23-0.68 in two clips
MIN_SPAN_PX = 6
MIN_JUDGED_SPAN_PX = 16  # smaller crops resolve dot-matrix gaps as missing rows
MIN_LIT_PX = 8


def led_row_diagnostics(crop_rgb: np.ndarray) -> dict | None:
    """Return row occupancy of saturated bright (LED-like) pixels, or None.

    None means the crop is too small or has no LED-like pixels (for example
    printed white numbers), so no claim is made about scan gaps.
    """
    if crop_rgb.ndim != 3 or crop_rgb.shape[2] != 3 or min(crop_rgb.shape[:2]) < MIN_SPAN_PX:
        return None
    rgb = crop_rgb.astype(np.float32)
    value = rgb.max(axis=2)
    saturation = (value - rgb.min(axis=2)) / np.maximum(value, 1) * 255
    threshold = max(140., float(np.percentile(value, 98)) * .55)
    lit = (value > threshold) & (saturation > 60)
    lit_px = int(lit.sum())
    rows = np.flatnonzero(lit.any(axis=1))
    if lit_px < MIN_LIT_PX or rows.size == 0 or rows[-1] - rows[0] + 1 < MIN_SPAN_PX:
        return None
    span = lit[rows[0]:rows[-1] + 1]
    occupancy = float(span.any(axis=1).mean())
    return {'version': DIAGNOSTIC_VERSION,
            'row_occupancy': round(occupancy, 3),
            'lit_ratio': round(lit_px / lit.size, 3),
            'span_px': int(span.shape[0]),
            # None: measured, but too small to judge.
            'scan_gap_suspected': (occupancy < SUSPECT_BELOW
                                   if span.shape[0] >= MIN_JUDGED_SPAN_PX else None)}
