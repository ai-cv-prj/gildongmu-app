"""실제 BoT-SORT의 저신뢰 연결·영상 격리·중복 제거를 검증한다."""
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from src.traffic_tracker import SignalTracker
from src.traffic_association import FrameContext, TemporalSelector
from src.visualization import draw_traffic
from test_traffic import fake_pipeline, FRAME, NEAR, FAR, CROSSWALK
from test_traffic_tracking import scene, shifted


def detections(xs=(100, 300), score=.8):
    return [dict(xyxy=[x, 100, x+20, 140], confidence=score,
                 class_id=0, class_name="pedestrian_signal") for x in xs]


class BOTSORTTests(unittest.TestCase):
    def test_motion_and_order_preserve_all_ids_and_current_boxes(self):
        tracker = SignalTracker()
        image = scene()
        first = detections()
        tracker.update(image, first, FrameContext(1, 0))
        self.assertEqual([s['track_id'] for s in first], [1, 2])
        for fid, dx in enumerate([40, 80, 120], 2):
            current = detections((300+dx, 100+dx))
            boxes = [list(s['xyxy']) for s in current]
            tracker.update(shifted(image, dx), current, FrameContext(fid, (fid-1)*200))
            self.assertEqual([s['track_id'] for s in current], [2, 1])
            self.assertEqual([s['xyxy'] for s in current], boxes)

    def test_low_confidence_only_recovers_existing_objects(self):
        for threshold, score in [(0.25, .15), (.4, .3)]:
            with self.subTest(threshold=threshold):
                tracker = SignalTracker()
                first = detections((100,))
                tracker.update(FRAME, first, FrameContext(1, 0, threshold))
                for fid in (2, 3):
                    current = detections((100+fid, 300), score)
                    tracker.update(FRAME, current, FrameContext(fid, (fid-1)*200, threshold))
                    self.assertEqual([s['track_id'] for s in current], [first[0]['track_id'], None])

    def test_blank_frame_hides_prediction_and_restores_strong_detection(self):
        tracker = SignalTracker()
        first = detections((100,))
        tracker.update(FRAME, first, FrameContext(1, 0))
        empty = []
        tracker.update(FRAME, empty, FrameContext(2, 200))
        self.assertEqual(empty, [])
        self.assertEqual(len(tracker.tracker.lost_stracks), 1)
        returned = detections((100,))
        tracker.update(FRAME, returned, FrameContext(3, 400))
        self.assertEqual(returned[0]['track_id'], first[0]['track_id'])

    def test_weak_detection_cannot_restore_id_after_blank_frame(self):
        tracker = SignalTracker()
        tracker.update(FRAME, detections((100,)), FrameContext(1, 0))
        tracker.update(FRAME, [], FrameContext(2, 200))
        weak = detections((100,), .15)
        tracker.update(FRAME, weak, FrameContext(3, 400))
        self.assertIsNone(weak[0]['track_id'])

    def test_lost_id_expires_by_capture_time_before_matching(self):
        for return_at, restored in [(3000, True), (3001, False)]:
            with self.subTest(return_at=return_at):
                tracker = SignalTracker()
                first = detections((100,))
                tracker.update(FRAME, first, FrameContext(1, 0))
                tracker.update(FRAME, [], FrameContext(2, 900))
                tracker.update(FRAME, [], FrameContext(3, 1700))
                tracker.update(FRAME, [], FrameContext(4, 2500))
                returned = detections((100,))
                tracker.update(FRAME, returned, FrameContext(5, return_at))
                self.assertEqual(returned[0]['track_id'] == first[0]['track_id'], restored)

    def test_ambiguous_or_weak_lost_match_gets_new_id(self):
        for initial, returned in [((100,), (94, 106)), ((94, 106), (100,)), ((100,), (110,))]:
            with self.subTest(initial=initial, returned=returned):
                tracker = SignalTracker()
                original = detections(initial)
                tracker.update(FRAME, original, FrameContext(1, 0))
                old_ids = {item['track_id'] for item in original}
                tracker.update(FRAME, [], FrameContext(2, 200))
                current = detections(returned)
                tracker.update(FRAME, current, FrameContext(3, 400))
                self.assertTrue(all(item['track_id'] not in old_ids for item in current))

    def test_short_occlusion_restores_ids_after_camera_motion_and_order_change(self):
        tracker = SignalTracker()
        image = scene()
        first = detections()
        tracker.update(image, first, FrameContext(1, 0))
        tracker.update(shifted(image, 40), [], FrameContext(2, 200))
        returned = detections((380, 180))
        tracker.update(shifted(image, 80), returned, FrameContext(3, 400))
        self.assertEqual([item['track_id'] for item in returned], [2, 1])

    def test_live_track_wins_over_ambiguous_lost_track(self):
        tracker = SignalTracker()
        first = detections((94, 106))
        tracker.update(FRAME, first, FrameContext(1, 0))
        tracker.update(FRAME, detections((106,)), FrameContext(2, 200))
        returned = detections((100,))
        tracker.update(FRAME, returned, FrameContext(3, 400))
        self.assertEqual(returned[0]['track_id'], first[1]['track_id'])

    def test_instances_do_not_reset_each_others_ids(self):
        first, second = SignalTracker(), SignalTracker()
        first.update(FRAME, detections((100,)), FrameContext(1, 0))
        other = detections()
        second.update(FRAME, other, FrameContext(1, 0))
        self.assertEqual([s['track_id'] for s in other], [1, 2])
        second.tracker.reset()
        current = detections()
        first.update(FRAME, current, FrameContext(2, 200))
        self.assertEqual([s['track_id'] for s in current], [1, 2])

    def test_frame_shape_or_time_discontinuity_does_not_reuse_ids(self):
        for fid, timestamp, shape in [(3, 200, (720, 1280)), (2, 1001, (720, 1280)),
                                      (2, -1, (720, 1280)), (2, 200, (360, 640))]:
            with self.subTest(fid=fid, timestamp=timestamp, shape=shape):
                tracker = SignalTracker()
                tracker.update(FRAME, detections((100,)), FrameContext(1, 0))
                current = detections((100,))
                tracker.update(cv2.resize(FRAME, shape[::-1]), current, FrameContext(fid, timestamp))
                self.assertEqual(current[0]['track_id'], 2)

    def test_selector_never_combines_different_ids_at_same_position(self):
        selector = TemporalSelector()
        for tid in (1, 2, 3):
            result = selector.update(dict(status='candidate', signal_index=0, crosswalk_index=0),
                                     [dict(xyxy=NEAR, track_id=tid)], [dict(xyxy=CROSSWALK)])
            self.assertIsNone(result['signal_index'])
            self.assertEqual(result['stable_frames'], 1)

    def test_current_id_cannot_be_replaced_by_nearby_different_ids(self):
        selector = TemporalSelector()
        selector.select(FRAME, [dict(xyxy=NEAR, track_id=1)], [], cv2, FrameContext(1, 0))
        result = selector.select(FRAME, [dict(xyxy=NEAR, track_id=2), dict(xyxy=FAR, track_id=3)],
                                 [], cv2, FrameContext(2, 200))
        self.assertIsNone(result['signal_index'])
        self.assertEqual(result['tracking']['reason'], 'target_missing')

    def test_missing_id_cannot_select_a_color_target(self):
        result = TemporalSelector().select(FRAME, [dict(xyxy=NEAR)], [], cv2, FrameContext(1, 0))
        self.assertIsNone(result['signal_index'])
        self.assertEqual(result['reason'], 'waiting_for_tracking')

    def test_low_score_keeps_target_without_triggering_multi_signal_selection(self):
        pipe = fake_pipeline([(NEAR, .8, 0)])
        first = pipe.predict(FRAME)
        moved = [602, 100, 642, 180]
        pipe.detector = fake_pipeline([(FAR, .2, 0), (moved, .15, 0)]).detector
        for _ in range(3):
            result = pipe.predict(FRAME)
            self.assertEqual(len(result['detections']), 1)
            self.assertEqual(result['detections'][0]['track_id'], first['detections'][0]['track_id'])
            self.assertEqual(result['detections'][0]['xyxy'], moved)
            self.assertEqual(result['association']['status'], 'tracked')
            self.assertEqual(result['signal_state'], 'green')
            self.assertEqual(result['raw_detected_signal_count'], 2)
            self.assertEqual(result['unmatched_low_confidence_count'], 1)
            self.assertEqual(result['suppressed_signal_count'], 0)
        self.assertEqual(pipe._classify.call_count, 4)

    def test_new_weak_signal_and_crosswalk_are_filtered(self):
        for threshold, score in [(0.25, .15), (.4, .3)]:
            with self.subTest(threshold=threshold):
                pipe = fake_pipeline([(NEAR, score, 0), (CROSSWALK, score, 1)])
                pipe.config['conf'] = threshold
                result = pipe.predict(FRAME)
                self.assertEqual(pipe.detector.predict.call_args.kwargs['conf'], .1)
                self.assertEqual(result['detections'], [])
                self.assertEqual(result['crosswalks'], [])
                self.assertEqual(result['unmatched_low_confidence_count'], 1)
                pipe._classify.assert_not_called()

    def test_explicit_lower_user_threshold_can_start_new_target(self):
        pipe = fake_pipeline([(NEAR, .08, 0)])
        pipe.config['conf'] = .05
        for _ in range(3):
            result = pipe.predict(FRAME)
            self.assertEqual(result['selected_detection_index'], 0)
            self.assertEqual(result['detections'][0]['track_id'], 1)
        self.assertEqual(pipe.detector.predict.call_args.kwargs['conf'], .05)

    def test_duplicate_nested_signal_does_not_trigger_multi_selection(self):
        pipe = fake_pipeline([(NEAR, .8, 0)])
        first = pipe.predict(FRAME)
        for items in [[([598, 98, 642, 182], .3, 0), (NEAR, .8, 0)],
                      [(NEAR, .8, 0), ([598, 98, 642, 182], .3, 0)]]:
            pipe.detector = fake_pipeline(items).detector
            result = pipe.predict(FRAME)
            self.assertEqual(result['detected_signal_count'], 1)
            self.assertEqual(result['raw_detected_signal_count'], 2)
            self.assertEqual(result['suppressed_signal_count'], 1)
            self.assertEqual(result['detections'][0]['xyxy'], NEAR)
            self.assertEqual(result['detections'][0]['track_id'], first['detections'][0]['track_id'])
            self.assertEqual(result['signal_state'], 'green')

    def test_separate_signals_and_crosswalks_are_not_deduplicated(self):
        pipe = fake_pipeline([(NEAR, .8, 0), (FAR, .3, 0), (CROSSWALK, .9, 1), (CROSSWALK, .8, 1)])
        result = pipe.predict(FRAME)
        self.assertEqual(result['detected_signal_count'], 2)
        self.assertEqual(result['suppressed_signal_count'], 0)
        self.assertEqual(len(result['crosswalks']), 2)
        self.assertEqual([s['track_id'] for s in result['detections']], [1, 2])

    def test_overlay_uses_same_candidate_label_as_realtime_ui(self):
        pipe = fake_pipeline([(NEAR, .8, 0), (FAR, .7, 0), (CROSSWALK, .9, 1)])
        with patch('src.traffic_association.estimate_vanishing_point', return_value=[1020, 280]):
            result = pipe.predict(FRAME)
        with patch('src.visualization.cv2.putText', wraps=cv2.putText) as draw:
            draw_traffic(FRAME, result)
        labels = [call.args[1] for call in draw.call_args_list]
        self.assertEqual(labels.count('SIGNAL CANDIDATE'), 2)

    def test_reset_restarts_ids_for_new_video(self):
        pipe = fake_pipeline([(NEAR, .8, 0)])
        pipe.predict(FRAME)
        pipe.detector = fake_pipeline([(FAR, .8, 0)]).detector
        result = pipe.predict(FRAME)
        self.assertEqual(result['detections'][0]['track_id'], 2)
        pipe.reset()
        result = pipe.predict(FRAME)
        self.assertEqual(result['detections'][0]['track_id'], 1)


if __name__ == '__main__':
    unittest.main()
