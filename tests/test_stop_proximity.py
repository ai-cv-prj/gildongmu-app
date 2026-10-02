"""Sequence checks for the stop proximity diagnostic."""

import unittest

from backend.stop_proximity import StopProximity


SHAPE = (100, 100, 3)


def detection(box, class_name="transit_stop", track_id=4, confidence=0.8):
    return {"xyxy": box, "class_name": class_name, "confidence": confidence,
            "track_id": track_id}


class StopProximityTests(unittest.TestCase):
    def test_confirms_same_near_stop_and_rejects_far_or_other_classes(self):
        monitor = StopProximity()
        self.assertEqual(monitor.update([detection([10, 10, 50, 50])], SHAPE, 1)["status"],
                         "not_detected")
        self.assertEqual(monitor.update([detection([10, 20, 60, 90], "bus")], SHAPE, 2)["status"],
                         "not_detected")
        stop = detection([10, 20, 60, 90])
        self.assertEqual(monitor.update([stop], SHAPE, 3)["status"], "candidate")
        self.assertEqual(monitor.update([stop], SHAPE, 3.5)["observations"], 2)
        confirmed = monitor.update([stop], SHAPE, 4)
        self.assertTrue(confirmed["nearby"])
        self.assertTrue(confirmed["newly_nearby"])
        self.assertEqual(confirmed["xyxy"], [0.1, 0.2, 0.6, 0.9])
        self.assertEqual(confirmed["basis"], "bottom")
        self.assertEqual(confirmed["arrival_event"]["type"], "stop_arrival")
        self.assertEqual(confirmed["arrival_event_id"], 1)
        self.assertFalse(monitor.update([stop], SHAPE, 4.5)["newly_nearby"])

    def test_large_stop_on_either_side_confirms_without_bottom_threshold(self):
        for box, expected_basis in (([0, 10, 25, 55], "left"),
                                    ([75, 10, 100, 55], "right")):
            with self.subTest(basis=expected_basis):
                monitor = StopProximity()
                for time in (1, 1.5):
                    self.assertEqual(monitor.update([detection(box)], SHAPE, time)["status"],
                                     "candidate")
                result = monitor.update([detection(box)], SHAPE, 2)
                self.assertTrue(result["nearby"])
                self.assertEqual(result["basis"], expected_basis)

    def test_side_rule_rejects_center_distant_and_small_boxes(self):
        for box in ([40, 10, 60, 55], [0, 35, 20, 55],
                    [0, 10, 6, 55], [0, 0, 20, 40]):
            with self.subTest(box=box):
                self.assertEqual(StopProximity().update([detection(box)], SHAPE, 1)["status"],
                                 "not_detected")

    def test_side_candidate_keeps_identity_and_can_follow_bottom_candidate(self):
        monitor = StopProximity()
        bottom = detection([10, 20, 40, 80], track_id=1)
        left = detection([0, 10, 25, 55], track_id=1)
        right = detection([75, 10, 100, 55], track_id=2)
        self.assertEqual(monitor.update([bottom], SHAPE, 1)["basis"], "bottom")
        self.assertEqual(monitor.update([left], SHAPE, 1.5)["observations"], 2)
        result = monitor.update([left], SHAPE, 2)
        self.assertEqual(result["status"], "nearby")
        self.assertEqual(result["basis"], "left")
        result = monitor.update([right], SHAPE, 2.5)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["observations"], 1)
        self.assertEqual(result["basis"], "right")

    def test_different_stop_and_frame_gap_require_fresh_confirmation(self):
        monitor = StopProximity()
        first = detection([5, 20, 45, 90], track_id=1)
        other = detection([60, 20, 95, 90], track_id=2)
        for time in (1, 1.5, 2):
            monitor.update([first], SHAPE, time)
        self.assertEqual(monitor.update([other], SHAPE, 2.5)["status"], "candidate")
        self.assertEqual(monitor.update([other], SHAPE, 5)["observations"], 1)
        held = monitor.update([other], SHAPE, 5.5, camera_view="unavailable")
        self.assertEqual(held["status"], "candidate")
        self.assertTrue(held["held"])
        self.assertEqual(monitor.update([other], SHAPE, 6)["observations"], 2)
        self.assertEqual(monitor.update([other], SHAPE, 6.5,
                                        state_reset=True)["status"], "not_detected")
        self.assertEqual(monitor.update([other], SHAPE, 7)["observations"], 1)

    def test_box_overlap_bridges_tracker_id_change_and_one_missing_frame(self):
        monitor = StopProximity()
        first = detection([10, 20, 60, 90], track_id=1)
        second = detection([11, 21, 61, 91], track_id=2)
        monitor.update([first], SHAPE, 1)
        self.assertEqual(monitor.update([second], SHAPE, 1.5)["observations"], 2)
        self.assertTrue(monitor.update([], SHAPE, 2)["held"])
        self.assertEqual(monitor.update([second], SHAPE, 2.5)["status"], "nearby")
        monitor.reset()
        self.assertEqual(monitor.update([second], SHAPE, 3)["observations"], 1)

    def test_confirmation_survives_missing_frame_but_requires_time_and_confidence(self):
        monitor = StopProximity()
        stop = detection([10, 20, 60, 90])
        self.assertEqual(monitor.update([detection(stop["xyxy"], confidence=.29)],
                                        SHAPE, 0)["status"], "not_detected")
        monitor.update([stop], SHAPE, .1)
        monitor.update([stop], SHAPE, .2)
        fast = monitor.update([stop], SHAPE, .3)
        self.assertEqual(fast["status"], "candidate")
        self.assertFalse(fast["arrival_recorded"])
        missing = monitor.update([], SHAPE, .4)
        self.assertEqual(missing["observations"], 3)
        self.assertIsNone(missing["xyxy"])
        confirmed = monitor.update([stop], SHAPE, .5)
        self.assertTrue(confirmed["newly_nearby"])
        self.assertTrue(confirmed["observed"])
        self.assertFalse(confirmed["held"])

    def test_candidate_memory_is_bounded_and_old_hits_do_not_confirm(self):
        monitor = StopProximity({"confirm_frames": 4})
        stop = detection([10, 20, 60, 90])
        for timestamp in (0, .9, 1.8, 2.7):
            result = monitor.update([stop], SHAPE, timestamp)
            self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["observations"], 3)
        monitor.update([], SHAPE, 3.5)
        expired = monitor.update([], SHAPE, 3.8)
        self.assertEqual(expired["status"], "not_detected")
        self.assertFalse(expired["arrival_recorded"])
        self.assertEqual(monitor.update([stop], SHAPE, 4)["observations"], 1)

    def test_confirmation_boundary_uses_client_epoch_timestamps(self):
        monitor = StopProximity()
        stop = detection([10, 20, 60, 90])
        for timestamp in (1790919300.2, 1790919300.4, 1790919300.6):
            result = monitor.update([stop], SHAPE, timestamp)
        self.assertEqual(result["status"], "nearby")
        self.assertIsNotNone(result["arrival_event"])

    def test_nearby_survives_view_and_detection_loss_without_stale_box_or_event(self):
        monitor = StopProximity()
        stop = detection([10, 20, 60, 90])
        for timestamp in (0, .2, .4):
            result = monitor.update([stop], SHAPE, timestamp)
        self.assertIsNotNone(result["arrival_event"])
        for timestamp, view in ((.6, "uncertain"), (.8, "unavailable"), (1, "clear")):
            result = monitor.update([], SHAPE, timestamp, camera_view=view)
            self.assertEqual(result["status"], "nearby")
            self.assertTrue(result["held"])
            self.assertFalse(result["observed"])
            self.assertIsNone(result["xyxy"])
            self.assertFalse(result["newly_nearby"])
            self.assertIsNone(result["arrival_event"])
        result = monitor.update([detection(stop["xyxy"], track_id=9)], SHAPE, 1.2)
        self.assertEqual(result["status"], "nearby")
        self.assertFalse(result["held"])
        self.assertIsNone(result["arrival_event"])

    def test_arrival_is_latched_across_long_loss_and_tracking_reset_until_new_session(self):
        monitor = StopProximity()
        stop = detection([10, 20, 60, 90])
        for timestamp in (0, .2, .4):
            monitor.update([stop], SHAPE, timestamp)
        for timestamp in (1, 2, 3, 3.5):
            expired = monitor.update([], SHAPE, timestamp)
        self.assertEqual(expired["status"], "not_detected")
        self.assertTrue(expired["arrival_recorded"])
        self.assertIsNone(expired["arrival_event"])
        for timestamp in (4, 4.2, 4.4):
            result = monitor.update([stop], SHAPE, timestamp)
            self.assertIsNone(result["arrival_event"])
        self.assertEqual(result["status"], "nearby")
        reset_result = monitor.update([stop], SHAPE, 4.6, state_reset=True)
        self.assertTrue(reset_result["arrival_recorded"])
        self.assertEqual(reset_result["observations"], 0)
        for timestamp in (4.8, 5, 5.2):
            result = monitor.update([stop], SHAPE, timestamp)
        self.assertEqual(result["status"], "nearby")
        self.assertIsNone(result["arrival_event"])
        monitor.reset()
        for timestamp in (0, .2, .4):
            result = monitor.update([stop], SHAPE, timestamp)
        self.assertIsNotNone(result["arrival_event"])

    def test_view_loss_cannot_create_arrival_or_keep_proximity_forever(self):
        monitor = StopProximity()
        stop = detection([10, 20, 60, 90])
        monitor.update([stop], SHAPE, 0)
        result = monitor.update([stop], SHAPE, .2, camera_view="uncertain")
        self.assertEqual(result["observations"], 1)
        self.assertFalse(result["arrival_recorded"])
        result = monitor.update([stop], SHAPE, 1.1, camera_view="unavailable")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["arrival_recorded"])

    def test_invalid_config_rejects_unbounded_or_impossible_evidence(self):
        for config in ({"nearby_hold_s": float("inf")}, {"min_confidence": True},
                       {"confirm_s": 3, "confirm_window_s": 2},
                       {"candidate_hold_s": 3, "confirm_window_s": 2}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                StopProximity(config)


if __name__ == "__main__":
    unittest.main()
