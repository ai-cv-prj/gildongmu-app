"""Missing bus IDs can recover only through separate, unambiguous geometry."""
import pytest

from backend.bus.bus_runtime.continuity import BusTrackContinuity
from backend.bus.base import InferenceContext
from test_bus_inference import make_frame, make_pipeline


BOX = (10, 10, 100, 150)


def observe(tracker, timestamp, entries):
    buses = [dict(track_id=track, bus_box=box) for track, box in entries]
    tracker.update(buses, timestamp)
    return buses


def test_one_missing_id_is_not_enough_and_same_time_is_not_a_second_observation():
    tracker = BusTrackContinuity()
    assert observe(tracker, 1, [(None, BOX)])[0]['track_id'] is None
    assert observe(tracker, 1, [(None, BOX)])[0]['track_id'] is None
    assert observe(tracker, .9, [(None, BOX)])[0]['track_id'] is None
    recovered, = observe(tracker, 1.4, [(None, BOX)])
    assert recovered['track_id'] < 0
    assert recovered['detector_track_id'] is None
    assert recovered['track_source'] == 'provisional'


def test_detector_id_changes_retain_unique_recent_identity():
    tracker = BusTrackContinuity()
    assert observe(tracker, 1, [(7, BOX)])[0]['track_id'] == 7
    missing, = observe(tracker, 1.4, [(None, BOX)])
    assert missing['track_id'] == 7
    changed, = observe(tracker, 1.8, [(12, BOX)])
    assert changed['track_id'] == 7
    assert changed['detector_track_id'] == 12
    assert changed['track_source'] == 'continuity'


def test_new_detector_id_can_take_over_a_provisional_vehicle():
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(None, BOX)])
    provisional = observe(tracker, 1.4, [(None, BOX)])[0]['track_id']
    assert observe(tracker, 1.8, [(9, BOX)])[0]['track_id'] == provisional


@pytest.mark.parametrize('later,box', [(2.01, BOX), (1.4, (110, 10, 200, 150))])
def test_expired_or_different_vehicle_cannot_inherit_identity(later, box):
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(7, BOX)])
    assert observe(tracker, later, [(9, box)])[0]['track_id'] == 9


def test_overlapping_untracked_buses_are_never_promoted():
    tracker = BusTrackContinuity()
    for t in (1, 1.4, 1.8, 2.2):
        buses = observe(tracker, t, [(None, BOX), (None, (12, 12, 102, 152))])
        assert all(bus['track_id'] is None for bus in buses)


def test_two_possible_previous_vehicles_do_not_share_their_evidence():
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(7, BOX), (8, (12, 12, 102, 152))])
    assert observe(tracker, 1.4, [(9, BOX)])[0]['track_id'] == 9


def test_two_new_vehicles_cannot_both_claim_one_previous_vehicle():
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(7, BOX)])
    buses = observe(tracker, 1.4, [(9, BOX), (10, BOX)])
    assert {b['track_id'] for b in buses} == {9, 10}


def test_two_live_detector_ids_previously_linked_split_without_duplicate_identity():
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(7, BOX)])
    observe(tracker, 1.4, [(9, BOX)])
    buses = observe(tracker, 1.8, [(7, BOX), (9, (110, 10, 200, 150))])
    assert {b['track_id'] for b in buses} == {7, 9}


def test_reset_and_expiration_drop_all_old_aliases():
    tracker = BusTrackContinuity()
    observe(tracker, 1, [(7, BOX)])
    observe(tracker, 1.4, [(9, BOX)])
    observe(tracker, 3, [])
    assert tracker._tracks == {} and tracker._aliases == {}
    tracker.reset()
    assert observe(tracker, 1, [(9, BOX)])[0]['track_id'] == 9


def test_pipeline_reads_an_untracked_bus_after_two_frames_and_preserves_ocr_metadata():
    pipeline = make_pipeline()
    pipeline.configure_target_route('604')
    pipeline.reset_session('one')
    pipeline.detector.tracks = [(None, BOX)]
    first = pipeline.infer(make_frame(), InferenceContext('one', 1, 1000))['event']
    assert first['matches'] == []
    second = pipeline.infer(make_frame(), InferenceContext('one', 2, 1400))['event']
    match, = second['matches']
    assert match['state'] == 'recognized_single'
    assert match['track_id'] < 0
    bus, = second['buses']
    assert bus['detector_track_id'] is None and bus['track_source'] == 'provisional'
    assert bus['observations'][0]['track_id'] == match['track_id']
    assert bus['observations'][0]['detector_track_id'] is None
    pipeline.reset_session('two')
    assert pipeline.infer(make_frame(), InferenceContext('two', 1, 1800))['event']['matches'] == []


def test_changed_detector_id_accumulates_independent_reads_on_the_same_vehicle():
    pipeline = make_pipeline()
    pipeline.recognizer.score = .95  # Must reach the original three-read policy.
    pipeline.configure_target_route('604')
    pipeline.reset_session('one')
    for frame, (tid, timestamp) in enumerate([(7,1000), (9,1200), (9,1400)],1):
        pipeline.detector.tracks = [(tid, BOX)]
        event = pipeline.infer(make_frame(), InferenceContext('one', frame, timestamp))['event']
        assert bool(event['matches']) == (frame == 3)
    assert event['matches'][0]['state'] == 'matched_candidate'
    assert event['matches'][0]['track_id'] == 7


def test_pipeline_duplicate_or_older_capture_cannot_create_provisional_evidence():
    pipeline = make_pipeline()
    pipeline.configure_target_route('604')
    pipeline.reset_session('one')
    pipeline.detector.tracks = [(None, BOX)]
    for frame, capture in enumerate([1000,1000,900],1):
        assert pipeline.infer(make_frame(), InferenceContext('one', frame, capture))['event']['matches'] == []
    assert pipeline.infer(make_frame(), InferenceContext('one', 4, 1400))['event']['matches']


def test_promoting_an_id_cannot_override_ineligible_ocr():
    pipeline = make_pipeline()
    pipeline.configure_target_route('604')
    pipeline.reset_session('one')
    pipeline.detector.tracks = [(None, BOX)]
    # A region on the bus search boundary stays ineligible even after ID recovery.
    pipeline.detector.text_regions = lambda rgb, box: (box, [(10,30,80,45)])
    for frame, capture in enumerate([1000,1400,1800],1):
        event = pipeline.infer(make_frame(), InferenceContext('one', frame, capture))['event']
        assert event['matches'] == []
    assert event['buses'][0]['track_id'] < 0
    assert event['buses'][0]['observations'][0]['eligible'] is False


def test_promoting_an_id_cannot_confirm_an_incomplete_scan():
    pipeline = make_pipeline()
    pipeline.configure_target_route('604')
    pipeline.reset_session('one')
    pipeline.detector.tracks = [(None, BOX)]
    pipeline.detector.text_regions = lambda rgb, box: (box, [(30,30,80,45)] * 25)
    for frame, capture in enumerate([1000,1400,1800],1):
        event = pipeline.infer(make_frame(), InferenceContext('one', frame, capture))['event']
        assert event['matches'] == []
    assert event['buses'][0]['track_id'] < 0
    assert event['buses'][0]['candidate_truncated'] is True
    assert event['buses'][0]['decision']['state'] == 'hold'
