"""Bus-only API pipeline contract with lightweight model substitutes."""
from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import unittest

import numpy as np

from backend.bus.base import InferenceContext, ModelSpec
from backend.bus.pipeline import BusPipeline


@dataclass(frozen=True)
class Prediction:
    """Model-free substitute for the PARSeq output contract."""
    text: str
    token_scores: list[float]
    elapsed_ms: float


class FakeDetector:
    def __init__(self):
        self.tracks = [(1, (10, 10, 100, 150)), (2, (110, 10, 195, 150))]
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1

    def sync(self):
        pass

    def buses(self, rgb):
        return [{'track_id': track_id, 'bus_box': box, 'bus_score': .95}
                for track_id, box in self.tracks]

    def text_regions(self, rgb, bus_box):
        x, y, _, _ = bus_box
        return bus_box, [(x+20, y+20, x+70, y+35)]


class FakeRecognizer:
    def __init__(self, *, fail=False, score=.995):
        self.fail = fail
        self.score = score

    def predict_batch(self, images):
        if self.fail:
            raise RuntimeError('OCR unavailable')
        values = []
        for image in images:
            text = '701' if float(np.asarray(image).mean()) > 120 else '604'
            values.append(Prediction(text, [self.score]*(len(text)+1), 1.0))
        return values


def make_frame():
    frame = np.full((180, 220, 3), 50, np.uint8)
    frame[30:45, 30:80] = 80
    frame[30:45, 130:180] = 190
    return frame


def make_pipeline():
    spec = ModelSpec(id='bus-route-display-b', mode='bus', name='B', version='B',
                     weights=Path('/unused/route-display-b.pt'))
    pipeline = BusPipeline(spec)
    pipeline.detector = FakeDetector()
    pipeline.recognizer = FakeRecognizer()
    return pipeline


class BusInferenceTests(unittest.TestCase):
    def test_per_bus_number_and_target_decision_are_separate(self):
        pipeline = make_pipeline()
        pipeline.configure_target_route('604')
        pipeline.reset_session('one')
        frame = make_frame()
        for frame_id, ms in enumerate((1000, 1200, 1400), 1):
            result = pipeline.infer(frame, InferenceContext('one', frame_id, ms))
        event = result['event']
        self.assertEqual(event['type'], 'bus_detection')
        self.assertEqual(event['target_route'], '604')
        self.assertEqual([(m['track_id'], m['state']) for m in event['matches']],
                         [(1, 'matched_candidate')])
        self.assertEqual(event['bus_number'], '604')
        self.assertTrue(event['is_target'])
        self.assertEqual([b['observations'][0]['text'] for b in event['buses']],
                         ['604', '701'])
        self.assertEqual(event['buses'][1]['decision']['state'], 'unverified')
        self.assertEqual(sum(d['class_name'] == 'bus' for d in result['detections']), 2)
        for detection in result['detections']:
            box = detection['box']
            self.assertTrue(0 <= box['x1'] < box['x2'] <= 1)
            self.assertTrue(0 <= box['y1'] < box['y2'] <= 1)

    def test_ocr_failure_keeps_bus_boxes_and_does_not_announce(self):
        pipeline = make_pipeline()
        pipeline.recognizer = FakeRecognizer(fail=True)
        pipeline.configure_target_route('604')
        pipeline.reset_session('one')
        with self.assertLogs('backend.bus.pipeline', level='ERROR'):
            result = pipeline.infer(make_frame(), InferenceContext('one', 1, 1000))
        self.assertEqual([d['class_name'] for d in result['detections']], ['bus', 'bus'])
        self.assertEqual(result['event']['matches'], [])
        self.assertIsNone(result['event']['bus_number'])
        self.assertFalse(result['event']['is_target'])
        self.assertTrue(result['event']['errors'][0].startswith('ocr_failed:'))

    def test_new_session_clears_track_history_and_target(self):
        pipeline = make_pipeline()
        pipeline.configure_target_route('604')
        pipeline.reset_session('one')
        for frame_id, ms in enumerate((1000, 1200, 1400), 1):
            pipeline.infer(make_frame(), InferenceContext('one', frame_id, ms))
        pipeline.close_session('one')
        pipeline.configure_target_route('701')
        pipeline.reset_session('two')
        result = pipeline.infer(make_frame(), InferenceContext('two', 1, 1600))
        self.assertEqual([(m['track_id'], m['route_number'], m['state'])
                          for m in result['event']['matches']],
                         [(2, '701', 'recognized_single')])
        self.assertEqual(result['event']['buses'][0]['decision']['state'], 'unverified')
        self.assertEqual(pipeline.detector.reset_count, 3)

    def test_no_target_still_recognizes_numbers_and_nonmonotonic_frame_is_held(self):
        pipeline = make_pipeline()
        pipeline.configure_target_route(None)
        pipeline.reset_session('one')
        result = pipeline.infer(make_frame(), InferenceContext('one', 1, 1000))
        self.assertEqual(result['event']['matches'], [])
        self.assertEqual(result['event']['buses'][0]['observations'][0]['text'], '604')
        self.assertEqual(result['event']['buses'][0]['decision']['state'], 'not_configured')
        pipeline.configure_target_route('604')
        pipeline.reset_session('two')
        pipeline.infer(make_frame(), InferenceContext('two', 1, 1200))
        late = pipeline.infer(make_frame(), InferenceContext('two', 2, 1100))
        self.assertEqual(late['event']['matches'], [])
        self.assertEqual(late['event']['buses'][0]['decision']['reason'],
                         'nonmonotonic_capture_time')


    def test_omitted_valid_region_prevents_target_announcement(self):
        class TooManyDetector(FakeDetector):
            def __init__(self):
                super().__init__()
                self.tracks = [(1, (10, 10, 100, 150))]

            def text_regions(self, rgb, bus_box):
                return bus_box, [(30, 30, 80, 45)]*25

        pipeline = make_pipeline()
        pipeline.detector = TooManyDetector()
        pipeline.configure_target_route('604')
        pipeline.reset_session('one')
        result = pipeline.infer(make_frame(), InferenceContext('one', 1, 1000))
        bus = result['event']['buses'][0]
        self.assertTrue(bus['candidate_truncated'])
        self.assertEqual(len(bus['observations']), 24)
        self.assertEqual(bus['decision']['state'], 'hold')
        self.assertEqual(result['event']['matches'], [])


if __name__ == '__main__':
    unittest.main()
