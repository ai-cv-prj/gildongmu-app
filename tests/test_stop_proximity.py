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
        self.assertFalse(monitor.update([stop], SHAPE, 4.5)["newly_nearby"])

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
