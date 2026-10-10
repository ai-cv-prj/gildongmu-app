"""The tested other-bus overlap gate changes evidence, not OCR or scan completeness."""
from copy import deepcopy
import unittest
from unittest import mock

from backend.bus.base import InferenceContext
from backend.bus.bus_runtime.evidence import gate_other_bus_overlap
from test_bus_inference import FakeDetector, FakeRecognizer, make_frame, make_pipeline


RISK = 'other_bus_bbox_overlap_risk'


class OtherBusOverlapTests(unittest.TestCase):
    def reading(self, *, eligible=True, reason='', bus_index=0):
        return {
            'bus_index': bus_index, 'track_id': 11,
            'text_box': (20, 20, 120, 40), 'text': '604',
            'token_scores': [.995] * 4, 'elapsed_ms': 1.5,
            'eligible': eligible, 'rejection_reason': reason,
            'context_checks': [], 'recovery_checks': [],
        }

    def buses(self, other_box, *, other_track=22):
        return [
            {'track_id': 11, 'bus_box': (0, 0, 140, 100)},
            {'track_id': other_track, 'bus_box': other_box},
        ]

    def test_threshold_is_number_region_fraction_not_bus_iou(self):
        # The other bus covers 5% of the number and much less of its own area.
        for other_x, rejected in ((115, True), (115.0001, False), (114.99, True)):
            with self.subTest(other_x=other_x):
                item = self.reading()
                gate_other_bus_overlap([item], self.buses((other_x, 0, 220, 100)))
                self.assertIs(item['eligible'], not rejected)
                if rejected:
                    self.assertEqual(item['visibility_rejection_reason'], RISK)

    def test_small_overlaps_from_separate_buses_are_not_summed(self):
        item = self.reading()
        buses = self.buses((117, 0, 220, 100))
        buses.append({'track_id': 33, 'bus_box': (0, 0, 23, 100)})
        gate_other_bus_overlap([item], buses)
        self.assertTrue(item['eligible'])

    def test_own_bus_is_excluded_by_index_even_with_equal_or_missing_track_ids(self):
        for track_id in (11, 22, None):
            with self.subTest(other_track=track_id):
                item = self.reading()
                buses = self.buses((115, 0, 220, 100), other_track=track_id)
                if track_id is None:
                    item['track_id'] = buses[0]['track_id'] = None
                gate_other_bus_overlap([item], buses)
                self.assertFalse(item['eligible'])
        item = self.reading(bus_index=1)
        buses = [
            {'track_id': 11, 'bus_box': (150, 0, 220, 100)},
            {'track_id': 11, 'bus_box': (0, 0, 140, 100)},
        ]
        gate_other_bus_overlap([item], buses)
        self.assertTrue(item['eligible'])

    def test_rejection_preserves_raw_ocr_geometry_and_existing_reason(self):
        item = self.reading()
        original = deepcopy(item)
        gate_other_bus_overlap([item], self.buses((115, 0, 220, 100)))
        self.assertEqual(item, {**original, 'eligible': False,
                                'visibility_rejection_reason': RISK})

        rejected = self.reading(eligible=False, reason='search_boundary_clipped')
        original = deepcopy(rejected)
        gate_other_bus_overlap([rejected], self.buses((115, 0, 220, 100)))
        self.assertEqual(rejected, original)

    def test_no_other_bus_or_no_intersection_keeps_the_reading_unchanged(self):
        for buses in (
            self.buses((150, 0, 220, 100)),
            self.buses((20, 50, 120, 100)),
            self.buses((115, 0, 220, 100))[:1],
        ):
            with self.subTest(buses=buses):
                item = self.reading()
                original = deepcopy(item)
                gate_other_bus_overlap([item], buses)
                self.assertEqual(item, original)

    def test_only_the_confirmed_duplicate_partner_is_exempt(self):
        item = self.reading()
        item.update(duplicate_bus_recovered=True, duplicate_bus_partner_index=1)
        original = deepcopy(item)
        buses = self.buses((0, 0, 140, 100))
        gate_other_bus_overlap([item], buses)
        self.assertEqual(item, original)
        # Another bus covering only the edge still vetoes the recovered read.
        buses.append({'track_id': 33, 'bus_box': (115, 0, 220, 100)})
        gate_other_bus_overlap([item], buses)
        self.assertFalse(item['eligible'])
        self.assertEqual(item['visibility_rejection_reason'], RISK)

    def test_duplicate_flag_alone_or_partner_without_recovery_is_not_an_exemption(self):
        for metadata in ({'duplicate_bus_recovered': True},
                         {'duplicate_bus_partner_index': 1},
                         {'duplicate_bus_recovered': False, 'duplicate_bus_partner_index': 1}):
            with self.subTest(metadata=metadata):
                item = {**self.reading(), **metadata}
                gate_other_bus_overlap([item], self.buses((0, 0, 140, 100)))
                self.assertFalse(item['eligible'])

    def test_previously_rejected_low_quality_duplicate_is_not_reenabled(self):
        item = self.reading(eligible=False, reason='fallback_low_token_score')
        item.update(token_scores=[.7] * 4, duplicate_bus_recovered=True,
                    duplicate_bus_partner_index=1)
        original = deepcopy(item)
        gate_other_bus_overlap([item], self.buses((0, 0, 140, 100)))
        self.assertEqual(item, original)


class EdgeOverlapDetector(FakeDetector):
    """Cover a number edge without putting its center in the other bus."""
    def __init__(self, *, overlap=True, source_right=80):
        super().__init__()
        self.overlap = overlap
        self.source_right = source_right

    def buses(self, rgb):
        self.tracks = [(1, (10, 10, 100, 150)),
                       (2, (77 if self.overlap else 110, 10, 195, 150))]
        return super().buses(rgb)

    def text_regions(self, rgb, bus_box):
        box = (30, 30, self.source_right, 45) if bus_box[0] == 10 else (130, 30, 180, 45)
        return bus_box, [box]


class CountingRecognizer(FakeRecognizer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inputs = []

    def predict_batch(self, images):
        self.inputs.extend((image.size, image.tobytes()) for image in images)
        return super().predict_batch(images)


class PipelineOtherBusOverlapTests(unittest.TestCase):
    def pipeline(self, *, overlap=True, target='604', score=.995, source_right=80):
        pipeline = make_pipeline()
        pipeline.detector = EdgeOverlapDetector(overlap=overlap, source_right=source_right)
        pipeline.recognizer = CountingRecognizer(score=score)
        pipeline.configure_target_route(target)
        pipeline.reset_session('overlap')
        return pipeline

    def infer(self, pipeline, frame_id=1, timestamp=1000):
        return pipeline.infer(make_frame(), InferenceContext('overlap', frame_id, timestamp))

    def test_high_confidence_single_target_is_not_returned_when_edge_is_covered(self):
        result = self.infer(self.pipeline())
        event = result['event']
        item = event['buses'][0]['observations'][0]
        self.assertEqual(item['text'], '604')
        self.assertGreaterEqual(item['token_score'], .98)
        self.assertEqual(item['rejection_reason'], '')
        self.assertEqual(item['visibility_rejection_reason'], RISK)
        self.assertFalse(item['eligible'])
        self.assertEqual(event['matches'], [])
        self.assertIsNone(event['bus_number'])
        self.assertFalse(event['is_target'])
        self.assertEqual([d['class_name'] for d in result['detections']],
                         ['bus', 'route_number', 'bus'])

    def test_repeated_non_target_covered_readings_cannot_be_announced(self):
        pipeline = self.pipeline(target='701')
        for frame_id, timestamp in enumerate((1000, 1200, 1400, 1600), 1):
            event = self.infer(pipeline, frame_id, timestamp)['event']
            self.assertFalse(any(route['track_id'] == 1 for route in event['recognized_routes']))
        self.assertTrue(event['is_target'])
        self.assertEqual(event['bus_number'], '701')

    def test_normal_other_bus_keeps_existing_target_and_observed_routes(self):
        pipeline = self.pipeline(overlap=False)
        for frame_id, timestamp in enumerate((1000, 1200, 1400), 1):
            event = self.infer(pipeline, frame_id, timestamp)['event']
        self.assertEqual(event['bus_number'], '604')
        self.assertEqual(event['matches'][0]['state'], 'matched_candidate')
        self.assertEqual([(route['track_id'], route['route_number'])
                          for route in event['recognized_routes']], [(1, '604'), (2, '701')])
        self.assertTrue(all(item['eligible'] for bus in event['buses']
                            for item in bus['observations']))

    def test_gate_does_not_skip_ocr_consume_budget_or_mark_scan_incomplete(self):
        clear = self.pipeline(overlap=False)
        covered = self.pipeline()
        clear_event = self.infer(clear)['event']
        with mock.patch.object(covered.matcher, 'update', wraps=covered.matcher.update) as update:
            covered_event = self.infer(covered)['event']
        self.assertEqual(clear.recognizer.inputs, covered.recognizer.inputs)
        self.assertEqual([size for size, _ in covered.recognizer.inputs[:2]],
                         [(50, 15), (50, 15)])
        for before, after in zip(clear_event['buses'], covered_event['buses']):
            for key in ('candidate_count', 'eligible_candidate_count', 'candidate_truncated'):
                self.assertEqual(before[key], after[key])
            self.assertFalse(after['candidate_truncated'])
        self.assertTrue(all(call.kwargs['complete'] for call in update.call_args_list))

    def test_rejected_regions_do_not_hide_an_exhausted_candidate_budget(self):
        class TooManyDetector(EdgeOverlapDetector):
            def text_regions(self, rgb, bus_box):
                search_box, boxes = super().text_regions(rgb, bus_box)
                return search_box, boxes * (25 if bus_box[0] == 10 else 1)

        pipeline = self.pipeline()
        pipeline.detector = TooManyDetector()
        with mock.patch.object(pipeline.matcher, 'update', wraps=pipeline.matcher.update) as update:
            event = self.infer(pipeline)['event']
        bus = event['buses'][0]
        self.assertEqual(bus['candidate_count'], 25)
        self.assertEqual(bus['eligible_candidate_count'], 25)
        self.assertEqual(len(bus['observations']), 24)
        self.assertTrue(bus['candidate_truncated'])
        self.assertFalse(update.call_args_list[0].kwargs['complete'])
        self.assertFalse(any(item['eligible'] for item in bus['observations']))
        self.assertEqual(event['matches'], [])

    def test_recovered_region_is_gated_after_recovery(self):
        pipeline = self.pipeline(source_right=70)

        def recover(rgb, current, buses, recognizer):
            source = current[0]
            self.assertTrue(source['eligible'])
            recovered = {**source, 'text_box': (30, 30, 85, 45),
                         'recovered_from': {'text': source['text'], 'box': source['text_box']}}
            source['eligible'] = False
            source['rejection_reason'] = 'superseded_by_full_region'
            current.append(recovered)

        with mock.patch('backend.bus.pipeline.recover_regions', side_effect=recover) as recovery:
            event = self.infer(pipeline)['event']
        recovery.assert_called_once()
        original, recovered = event['buses'][0]['observations']
        self.assertEqual(original['rejection_reason'], 'superseded_by_full_region')
        self.assertEqual(recovered['visibility_rejection_reason'], RISK)
        self.assertFalse(recovered['eligible'])
        self.assertEqual(event['matches'], [])

    def test_actual_duplicate_recovery_keeps_one_high_quality_route(self):
        class DuplicateDetector(FakeDetector):
            def __init__(self):
                super().__init__()
                self.tracks = [(1, (10, 10, 100, 150)), (2, (13, 10, 103, 150))]

            def text_regions(self, rgb, bus_box):
                return bus_box, [(30, 30, 80, 45)]

        pipeline = self.pipeline()
        pipeline.detector = DuplicateDetector()
        event = self.infer(pipeline)['event']
        self.assertEqual(event['bus_number'], '604')
        self.assertEqual(len(event['matches']), 1)
        observations = [item for bus in event['buses'] for item in bus['observations']]
        winner = next(item for item in observations if item['eligible'])
        self.assertTrue(winner['duplicate_bus_recovered'])
        self.assertEqual(winner['duplicate_bus_partner_index'], 1)
        self.assertEqual(winner['text'], '604')
        self.assertEqual(sum(item['eligible'] for item in observations), 1)

    def test_temporary_rejection_keeps_valid_target_and_non_target_history(self):
        for target in ('604', '701'):
            with self.subTest(target=target):
                pipeline = self.pipeline(overlap=False, target=target, score=.95)
                for frame_id, timestamp in enumerate((1000, 1200), 1):
                    self.infer(pipeline, frame_id, timestamp)
                pipeline.detector.overlap = True
                blocked = self.infer(pipeline, 3, 1400)['event']
                self.assertFalse(any(route['track_id'] == 1
                                     for route in blocked['recognized_routes']))
                pipeline.detector.overlap = False
                restored = self.infer(pipeline, 4, 1600)['event']
                route = next(route for route in restored['recognized_routes']
                             if route['track_id'] == 1)
                self.assertEqual(route['route_number'], '604')
                self.assertEqual(route['state'], 'matched_candidate')
                if target == '604':
                    self.assertEqual(restored['buses'][0]['decision']['support_samples'], 3)


if __name__ == '__main__':
    unittest.main()
