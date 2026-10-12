"""Sparse correct reads accumulate while the same vehicle remains observed."""
import pytest

from backend.bus.bus_runtime.target import TargetMatcher
from backend.bus.bus_runtime.observed import ObservedRouteMatcher
from backend.bus.base import InferenceContext
from test_bus_inference import FakeRecognizer, Prediction, make_frame, make_pipeline


BOX = (10,10,100,150)


def reading(text='7713', score=.99):
    return dict(text=text, token_scores=[score] * (len(text)+1), eligible=True)


@pytest.mark.parametrize('other', [False, True])
def test_three_reads_across_continuously_observed_bus_can_span_nearly_two_seconds(other):
    target = TargetMatcher('7713')
    observed = ObservedRouteMatcher()
    for at in [0, .4, .848, 1.2, 1.6, 1.964]:
        obs = [reading()] if at in [0, .848, 1.964] else []
        result = (observed.update(7,at,obs,BOX,target='272',complete=True) if other
                  else target.update(7,at,obs,BOX))
    if other:
        assert result[0]['route_number'] == '7713'
        assert result[0]['state'] == 'matched_candidate'
    else:
        assert result['state'] == 'matched_candidate' and result['support_samples'] == 3


@pytest.mark.parametrize('problem', ['expired','lost','incomplete','rival','missing_current'])
def test_wider_window_does_not_confirm_expired_lost_or_conflicting_evidence(problem):
    matcher = TargetMatcher('7713')
    times = [0, .4, .848, 1.2, 1.6, 1.964]
    if problem == 'expired': times[-1] = 2.01
    if problem == 'lost': times = [0, .4, .848, 1.964]
    for at in times:
        obs = [reading()] if at in [0, .848, times[-1]] else []
        if problem == 'rival' and at == 1.6: obs = [reading('7712')]
        if problem == 'missing_current' and at == times[-1]: obs = []
        complete = not (problem == 'incomplete' and at == 1.2)
        result = matcher.update(7,at,obs,BOX,complete=complete)
    assert result['state'] != 'matched_candidate'


@pytest.mark.parametrize('interval_ms', [610, 750, 900])
def test_slow_consecutive_frames_confirm_target_and_other_route(interval_ms):
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.configure_target_route('604')
    pipeline.reset_session('slow')
    for index in range(3):
        event = pipeline.infer(make_frame(), InferenceContext('slow', index + 1, 1000 + index * interval_ms))['event']
        if index < 2:
            assert event['matches'] == []
            assert event['recognized_routes'] == []
    assert [(item['route_number'], item['state']) for item in event['matches']] == [('604', 'matched_candidate')]
    assert {(item['route_number'], item['is_target']) for item in event['recognized_routes']} == {('604', True), ('701', False)}


def test_slow_spatially_verified_detector_id_change_preserves_canonical_vehicle():
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.configure_target_route('604')
    pipeline.reset_session('slow')
    for index, (track_id, timestamp) in enumerate([(7, 1000), (9, 1800), (9, 2600)], 1):
        pipeline.detector.tracks = [(track_id, BOX)]
        event = pipeline.infer(make_frame(), InferenceContext('slow', index, timestamp))['event']
    assert event['matches'][0]['state'] == 'matched_candidate'
    assert event['matches'][0]['track_id'] == 7


@pytest.mark.parametrize('target', ['604', '999'])
@pytest.mark.parametrize('loss', ['missing_bus', 'failed_frame'])
def test_observed_loss_requires_three_new_reads_even_when_same_detector_id_returns(target, loss):
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.detector.tracks = [(1, BOX)]
    pipeline.configure_target_route(target)
    pipeline.reset_session('loss')
    for index, timestamp in enumerate([1000, 1800], 1):
        pipeline.infer(make_frame(), InferenceContext('loss', index, timestamp))
    if loss == 'missing_bus':
        pipeline.detector.tracks = []
        event = pipeline.infer(make_frame(), InferenceContext('loss', 3, 2200))['event']
        assert event['recognized_routes'] == []
    else:
        detect = pipeline.detector.buses
        def fail(_rgb):
            raise RuntimeError('detector unavailable')
        pipeline.detector.buses = fail
        with pytest.raises(RuntimeError, match='detector unavailable'):
            pipeline.infer(make_frame(), InferenceContext('loss', 3, 2200))
        pipeline.detector.buses = detect
    pipeline.detector.tracks = [(1, BOX)]
    for index, timestamp in enumerate([2600, 3400, 4200], 4):
        event = pipeline.infer(make_frame(), InferenceContext('loss', index, timestamp))['event']
        assert bool(event['recognized_routes']) == (index == 6)
    assert event['recognized_routes'][0]['route_number'] == '604'


def test_new_canonical_vehicle_does_not_inherit_slow_readings():
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.configure_target_route('604')
    pipeline.reset_session('changed')
    frame = make_frame()
    frame[30:45, 130:180] = 80  # Another vehicle can display the same route.
    pipeline.detector.tracks = [(1, BOX)]
    for index, timestamp in enumerate([1000, 1800], 1):
        pipeline.infer(frame, InferenceContext('changed', index, timestamp))
    pipeline.detector.tracks = [(2, (110, 10, 195, 150))]
    for index, timestamp in enumerate([2600, 3400, 4200], 3):
        event = pipeline.infer(frame, InferenceContext('changed', index, timestamp))['event']
        assert bool(event['matches']) == (index == 5)
        assert event['buses'][0]['decision']['support_samples'] == index - 2
    assert event['matches'][0]['track_id'] == 2


def test_gap_beyond_spatial_continuity_limit_requires_new_evidence():
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.detector.tracks = [(1, BOX)]
    pipeline.configure_target_route('604')
    pipeline.reset_session('gap')
    for index, timestamp in enumerate([1000, 1400, 2500], 1):
        event = pipeline.infer(make_frame(), InferenceContext('gap', index, timestamp))['event']
        assert event['matches'] == []
    assert event['buses'][0]['decision']['support_samples'] == 1


def test_continuous_detection_keeps_two_second_limit_and_requires_current_full_number():
    pipeline = make_pipeline()
    pipeline.recognizer = FakeRecognizer(score=.95)
    pipeline.detector.tracks = [(1, BOX)]
    pipeline.configure_target_route('604')
    pipeline.reset_session('window')
    regions = pipeline.detector.text_regions
    for index, timestamp in enumerate([1000, 1800, 2600, 3400], 1):
        pipeline.detector.text_regions = (lambda rgb, box: (box, [])) if index == 3 else regions
        event = pipeline.infer(make_frame(), InferenceContext('window', index, timestamp))['event']
        assert event['matches'] == []
    assert event['buses'][0]['decision']['support_samples'] == 2


@pytest.mark.parametrize('problem', ['partial', 'conflict', 'jump', 'incomplete'])
def test_slow_frames_still_reject_partial_conflicting_jumped_or_incomplete_evidence(problem):
    pipeline = make_pipeline()
    pipeline.detector.tracks = [(1, BOX)]
    pipeline.configure_target_route('604')
    pipeline.reset_session('blocked')
    text = ['604']
    pipeline.recognizer.predict_batch = lambda images: [Prediction(text[0], [.95] * (len(text[0]) + 1), 1.0) for _ in images]
    for index, timestamp in enumerate([1000, 1800, 2600], 1):
        if index == 3:
            if problem == 'partial': text[0] = '60'
            if problem == 'conflict': text[0] = '605'
            if problem == 'jump': pipeline.detector.tracks = [(1, (110, 10, 195, 150))]
            if problem == 'incomplete': pipeline.detector.text_regions = lambda rgb, box: (box, [(30,30,80,45)] * 25)
        event = pipeline.infer(make_frame(), InferenceContext('blocked', index, timestamp))['event']
        assert event['matches'] == []
    assert event['buses'][0]['decision']['state'] == {'partial': 'partial', 'conflict': 'conflict', 'jump': 'hold', 'incomplete': 'hold'}[problem]


def test_slow_other_route_keeps_longer_number_context_while_bus_is_continuously_visible():
    pipeline = make_pipeline()
    pipeline.detector.tracks = [(1, BOX)]
    pipeline.configure_target_route('999')
    pipeline.reset_session('other')
    text = ['1711']
    pipeline.recognizer.predict_batch = lambda images: [Prediction(text[0], [.95] * (len(text[0]) + 1), 1.0) for _ in images]
    for index, timestamp in enumerate([1000, 1800, 2600, 3400, 4200], 1):
        if index > 1: text[0] = '171'
        event = pipeline.infer(make_frame(), InferenceContext('other', index, timestamp))['event']
        assert event['recognized_routes'] == []
