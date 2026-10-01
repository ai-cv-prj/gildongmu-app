"""Sequence checks for the stop proximity diagnostic."""

import unittest

from backend.stop_proximity import StopProximity


SHAPE = (100, 100, 3)


def detection(box, class_name="transit_stop", track_id=4):
    return {"xyxy": box, "class_name": class_name, "confidence": 0.8,
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

    def test_different_stop_gap_and_view_loss_require_fresh_confirmation(self):
        monitor = StopProximity()
        first = detection([5, 20, 45, 90], track_id=1)
        other = detection([60, 20, 95, 90], track_id=2)
        for time in (1, 1.5, 2):
            monitor.update([first], SHAPE, time)
        self.assertEqual(monitor.update([other], SHAPE, 2.5)["status"], "candidate")
        self.assertEqual(monitor.update([other], SHAPE, 5)["observations"], 1)
        self.assertEqual(monitor.update([other], SHAPE, 5.5,
                                        camera_view="unavailable")["status"], "unavailable")
        self.assertEqual(monitor.update([other], SHAPE, 6)["observations"], 1)
        self.assertEqual(monitor.update([other], SHAPE, 6.5,
                                        state_reset=True)["status"], "not_detected")
        self.assertEqual(monitor.update([other], SHAPE, 7)["observations"], 1)

    def test_box_overlap_bridges_tracker_id_change_but_empty_frame_clears(self):
        monitor = StopProximity()
        first = detection([10, 20, 60, 90], track_id=1)
        second = detection([11, 21, 61, 91], track_id=2)
        monitor.update([first], SHAPE, 1)
        self.assertEqual(monitor.update([second], SHAPE, 1.5)["observations"], 2)
        self.assertEqual(monitor.update([], SHAPE, 2)["status"], "not_detected")
        self.assertEqual(monitor.update([second], SHAPE, 2.5)["observations"], 1)
        monitor.reset()
        self.assertEqual(monitor.update([second], SHAPE, 3)["observations"], 1)


if __name__ == "__main__":
    unittest.main()
