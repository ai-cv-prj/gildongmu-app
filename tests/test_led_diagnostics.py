"""Diagnostic-only LED scan-gap measurement on synthetic route-number crops."""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from backend.bus.base import InferenceContext
from backend.bus.bus_runtime.led_diagnostics import led_row_diagnostics
from test_bus_inference import make_frame, make_pipeline

AMBER = (255, 170, 30)


def led_crop(lit_rows):
    crop = np.full((24, 60, 3), 20, np.uint8)
    for row in lit_rows:
        crop[row, 5:55] = AMBER
    return crop


class LedDiagnosticsTests(unittest.TestCase):
    def test_fully_lit_rows_are_not_suspected(self):
        result = led_row_diagnostics(led_crop(range(3, 21)))
        self.assertEqual(result['row_occupancy'], 1.0)
        self.assertFalse(result['scan_gap_suspected'])

    def test_dot_matrix_gaps_stay_above_threshold(self):
        # A clean dot matrix can leave a dark line between most LED rows.
        rows = [r for r in range(3, 21) if r % 5 != 0]
        self.assertFalse(led_row_diagnostics(led_crop(rows))['scan_gap_suspected'])

    def test_few_scan_lines_are_suspected(self):
        result = led_row_diagnostics(led_crop([3, 4, 12, 20]))
        self.assertLess(result['row_occupancy'], .8)
        self.assertTrue(result['scan_gap_suspected'])

    def test_unsaturated_or_tiny_crops_make_no_claim(self):
        white = np.full((24, 60, 3), 20, np.uint8)
        white[3:21, 5:55] = 240
        self.assertIsNone(led_row_diagnostics(white))
        self.assertIsNone(led_row_diagnostics(led_crop([1])[:4]))
        self.assertIsNone(led_row_diagnostics(np.zeros((24, 60, 3), np.uint8)))

    def test_small_span_is_measured_but_not_judged(self):
        result = led_row_diagnostics(led_crop([3, 6, 9, 12]))
        self.assertLess(result['row_occupancy'], .8)
        self.assertIsNone(result['scan_gap_suspected'])


class PipelineLedDiagnosticsTests(unittest.TestCase):
    def run_once(self):
        pipeline = make_pipeline()
        pipeline.configure_target_route('604')
        pipeline.reset_session('one')
        return pipeline.infer(make_frame(), InferenceContext('one', 1, 1000))['event']

    def test_observations_carry_diagnostics_without_changing_ocr(self):
        event = self.run_once()
        observations = [o for b in event['buses'] for o in b['observations']]
        self.assertEqual([o['text'] for o in observations], ['604', '701'])
        self.assertTrue(all('led_diagnostics' in o for o in observations))

    def test_diagnostic_failure_keeps_ocr_result(self):
        with mock.patch('backend.bus.pipeline.led_row_diagnostics', side_effect=RuntimeError('boom')):
            event = self.run_once()
        observations = [o for b in event['buses'] for o in b['observations']]
        self.assertEqual([o['text'] for o in observations], ['604', '701'])
        self.assertTrue(all(o['led_diagnostics'] is None for o in observations))
        self.assertEqual(event['errors'], [])


if __name__ == '__main__':
    unittest.main()
