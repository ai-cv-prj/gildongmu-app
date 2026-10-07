"""Confirmed near obstacles retain warnings while their contact leaves the frame."""
import unittest

from src.walking_voice import walking_action
from test_risk import FRAME, detection, engine


class ClippedNearContactTests(unittest.TestCase):
    def bollard(self, box=(40, 60, 60, 98)):
        return detection(box, name="bollard", cid=20)

    def test_observed_clipping_keeps_warning_and_voice_beyond_uncertainty_timeout(self):
        for identity in (1, None):
            with self.subTest(identity=identity):
                risk = engine(identity)
                first = risk.update(FRAME, [self.bollard()], 0)["detections"][0]
                self.assertEqual(first["risk_level"], "danger")
                for step in range(1, 16):
                    result = risk.update(FRAME, [self.bollard((40, 60, 60, 100))], step / 10)
                    item = result["detections"][0]
                    self.assertEqual(item["event_id"], first["event_id"])
                    self.assertEqual(item["risk_level"], "caution")
                    self.assertEqual(item["proximity"]["band"], "unknown")
                    self.assertFalse(item["proximity"]["contact_reliable"])
                    self.assertEqual(item["alert_level"], "danger")
                    self.assertEqual(item["hold_reason"], "near_contact_bottom_clipped")
                    self.assertIsNotNone(walking_action(result, 100))

    def test_unique_id_bridge_preserves_confirmed_near_contact(self):
        risk = engine(1)
        first = risk.update(FRAME, [self.bollard()], 0)["detections"][0]
        risk.tracker.identity = 2
        item = risk.update(FRAME, [self.bollard((40, 60, 60, 100))], .1)["detections"][0]
        self.assertTrue(item["event_identity_bridged"])
        self.assertEqual(item["event_id"], first["event_id"])
        self.assertEqual(item["hold_reason"], "near_contact_bottom_clipped")

    def test_one_missing_frame_does_not_split_a_uniquely_matched_clipped_object(self):
        risk = engine()
        first = risk.update(FRAME, [self.bollard()], 0)["detections"][0]
        risk.tracker.identity = None
        risk.update(FRAME, [self.bollard((40, 60, 60, 100))], .1)
        risk.update(FRAME, [], .3)
        risk.tracker.identity = 2
        item = risk.update(FRAME, [self.bollard((40, 60, 60, 100))], .5)["detections"][0]
        self.assertEqual(item["event_id"], first["event_id"])
        self.assertEqual(item["hold_reason"], "near_contact_bottom_clipped")

    def test_first_clipped_detection_and_unrelated_object_do_not_inherit_danger(self):
        risk = engine(1)
        item = risk.update(FRAME, [self.bollard((40, 60, 60, 100))], 0)["detections"][0]
        self.assertEqual(item["alert_level"], "caution")
        risk.reset()
        first = risk.update(FRAME, [self.bollard()], 0)["detections"][0]
        risk.tracker.identity = 2
        item = risk.update(FRAME, [self.bollard((70, 60, 90, 100))], .1)["detections"][0]
        self.assertNotEqual(item["event_id"], first["event_id"])
        self.assertEqual(item["alert_level"], "caution")

    def test_departure_from_priority_path_does_not_refresh_clipped_warning(self):
        risk = engine(config={"wide_roi_priority_enabled": True})
        risk.update(FRAME, [self.bollard()], 0)
        for step in range(1, 13):
            item = risk.update(FRAME, [self.bollard((0, 60, 5, 100))], step / 10)["detections"][0]
            self.assertNotEqual(item["hold_reason"], "near_contact_bottom_clipped")
        self.assertNotEqual(item["alert_level"], "danger")

    def test_valid_lower_proximity_can_release_retained_warning(self):
        risk = engine()
        risk.update(FRAME, [self.bollard()], 0)
        for step in range(1, 13):
            risk.update(FRAME, [self.bollard((40, 60, 60, 100))], step / 10)
        for step in range(13, 25):
            item = risk.update(FRAME, [self.bollard((40, 40, 60, 75))], step / 10)["detections"][0]
            self.assertNotIn("near_contact_bottom_clipped", item["reasons"])
        self.assertNotEqual(item["alert_level"], "danger")

    def test_visibility_loss_and_reset_discard_contact_memory(self):
        for reset in (False, True):
            with self.subTest(reset=reset):
                risk = engine()
                risk.update(FRAME, [self.bollard()], 0)
                if reset:
                    risk.reset()
                else:
                    for time in (.2, .4, .6):
                        risk.update(FRAME, [], time)
                item = risk.update(FRAME, [self.bollard((40, 60, 60, 100))], .7)["detections"][0]
                self.assertEqual(item["alert_level"], "caution")


if __name__ == "__main__":
    unittest.main()
