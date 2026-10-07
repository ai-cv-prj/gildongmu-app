"""Speech evidence for another visible bus stays separate from the target match."""
import pytest

from backend.bus.bus_runtime.observed import ObservedRouteMatcher
from test_bus_inference import make_frame, make_pipeline
from backend.bus.base import InferenceContext


BOX = (0, 0, 100, 100)


def reading(text, score=.99, eligible=True):
    return {'text': text, 'token_scores': [score] * (len(text) + 1), 'eligible': eligible}


def observe(matcher, timestamp, observations, track=1, box=BOX, complete=True, target='7011'):
    return matcher.update(track, timestamp, observations, box, target=target, complete=complete)


def test_other_bus_requires_repeated_current_readings_and_keeps_target_contract():
    pipeline = make_pipeline()
    pipeline.configure_target_route('604')
    pipeline.reset_session('session')
    for frame_id, ms in enumerate((1000, 1200, 1400), 1):
        result = pipeline.infer(make_frame(), InferenceContext('session', frame_id, ms, .25))
        other = [item for item in result['event']['recognized_routes'] if not item['is_target']]
        assert len(other) == (1 if frame_id == 3 else 0)
    assert other[0]['route_number'] == '701'
    assert other[0]['state'] == 'matched_candidate'
    assert result['event']['bus_number'] == '604'
    assert [item['route_number'] for item in result['event']['matches']] == ['604']
    pipeline.reset_session('next')
    result = pipeline.infer(make_frame(), InferenceContext('next', 1, 1600, .25))
    assert all(item['is_target'] for item in result['event']['recognized_routes'])


@pytest.mark.parametrize('observations', [
    [reading('701')], [reading('11')], [reading('7011')],
    [reading('1')], [reading('711')], [reading('71')],
    [reading('173', .89)], [reading('173', eligible=False)],
    [reading('173'), reading('174')], [reading('173'), reading('7011')],
    [reading('전화')], [reading('173', float('nan'))],
])
def test_partial_target_weak_ambiguous_or_invalid_readings_do_not_speak(observations):
    matcher = ObservedRouteMatcher()
    for timestamp in (1, 1.2, 1.4, 1.6):
        assert observe(matcher, timestamp, observations) == []


@pytest.mark.parametrize('invalid', ['duplicate_time', 'gap', 'jump', 'incomplete', 'untracked', 'missing'])
def test_lost_current_evidence_cannot_confirm_another_bus(invalid):
    matcher = ObservedRouteMatcher()
    for timestamp in (1, 1.2):
        assert observe(matcher, timestamp, [reading('173')]) == []
    kwargs = {}
    timestamp = 1.4
    observations = [reading('173')]
    if invalid == 'duplicate_time': timestamp = 1.2
    if invalid == 'gap': timestamp = 2
    if invalid == 'jump': kwargs['box'] = (200, 200, 300, 300)
    if invalid == 'incomplete': kwargs['complete'] = False
    if invalid == 'untracked': kwargs['track'] = None
    if invalid == 'missing': observations = []
    assert observe(matcher, timestamp, observations, **kwargs) == []


def test_short_reading_after_longer_route_is_not_a_new_confirmed_number():
    matcher = ObservedRouteMatcher()
    observe(matcher, 1, [reading('1711')])
    for timestamp in (1.2, 1.4, 1.6, 1.8, 2, 2.2, 2.4, 2.6, 2.8, 3):
        assert observe(matcher, timestamp, [reading('171')]) == []


def test_longer_number_context_expires_when_track_is_lost():
    matcher = ObservedRouteMatcher()
    observe(matcher, 1, [reading('1711')])
    for timestamp in (2, 2.2):
        assert observe(matcher, timestamp, [reading('171')]) == []
    assert observe(matcher, 2.4, [reading('171')])[0]['route_number'] == '171'


def test_nonmonotonic_frames_cannot_publish_observed_confirmation():
    pipeline = make_pipeline()
    pipeline.configure_target_route('7011')
    pipeline.reset_session('session')
    for frame_id, ms in enumerate((1000, 1200, 1400), 1):
        pipeline.infer(make_frame(), InferenceContext('session', frame_id, ms, .25))
    result = pipeline.infer(make_frame(), InferenceContext('session', 4, 1300, .25))
    assert result['event']['recognized_routes'] == []
