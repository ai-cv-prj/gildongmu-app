"""Archived worker snapshots are replayed once, before evaluating clip windows."""
import json

from scripts import evaluate_bus_postprocess as replay
from backend.bus.bus_runtime.target import TargetMatcher


def event(number='172', capture=1000, frame=1):
    return dict(frame_id=frame, captured_at_ms=capture, event={
        'target_route': number, 'buses': [{
            'track_id': 7, 'box': dict(x1=.1, y1=.1, x2=.5, y2=.5),
            'candidate_truncated': False, 'observations': [{
                'text': number, 'token_scores': [.95] * (len(number) + 1), 'eligible': True,
            }],
        }],
    })


def test_cached_snapshots_do_not_add_independent_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, 'ROOT', tmp_path)
    monkeypatch.setattr(replay, 'review', dict(clips=[dict(source_id='member1', session_id='session')]))
    path = tmp_path/'data/result-hub/sources/member1/sessions/session/results.jsonl'
    path.parent.mkdir(parents=True)
    rows = [dict(bus=event(frame=1, capture=1000)), dict(bus=event(frame=1, capture=1000)),
            dict(bus=event(frame=2, capture=1400))]
    path.write_text('\n'.join(json.dumps(x) for x in rows))
    records = replay.load_sessions()[('member1', 'session')]
    assert len(records) == 2
    assert [r['line'] for r in records] == [1, 3]


def test_session_reads_before_clip_start_can_confirm_a_current_read():
    key = ('member1', 'session')
    records = [dict(bus=event(capture=t, frame=i)) for i, t in enumerate([1000, 1200, 1400], 1)]
    predictions = replay.replay({key: records})
    final = predictions[(key, 3, 1400)]['routes']
    assert final[0]['state'] == 'matched_candidate'


def test_experimental_timing_does_not_mutate_the_production_matcher():
    experimental, _ = replay.matcher_types(window=1.5, gap=1)
    original = TargetMatcher('172')
    changed = experimental('172')
    observations = event()['event']['buses'][0]['observations']
    for t in [0, .842, 1.237]:
        original_result = original.update(7, t, observations)
        changed_result = changed.update(7, t, observations)
    assert original_result['state'] != 'matched_candidate'
    assert changed_result['state'] == 'matched_candidate'
    assert TargetMatcher('172').window_s == 2.0


def test_wrong_route_on_a_labeled_vehicle_is_counted_as_an_error(monkeypatch):
    sample = dict(source_in_clip=True, source_offset_ms=0, bus_frame_id=1, bus_result_age_ms=200)
    clip = dict(source_id='member1', session_id='session', started_at_ms=1000, samples=[sample])
    monkeypatch.setattr(replay, 'review', dict(clips=[clip]))
    monkeypatch.setattr(replay, 'labels', dict(encounters=[dict(
        encounter_id='01-172', clip_no=1, track_ids=[7], ground_truth_number='172', boarding='approach')]))
    predictions = {(('member1', 'session'), 1, 1000): dict(routes=[dict(
        origin_track_id=7, track_id=7, route_number='173', state='matched_candidate', is_target=False)])}
    result = replay.evaluate(predictions)
    assert result['accepted'] == 0 and result['confirmed'] == 0
    assert len(result['false_outputs']) == 1
    assert result['unknown_outputs'] == []


def test_missing_receipt_time_does_not_become_zero_delay_or_a_recognition_failure(monkeypatch):
    sample = dict(source_in_clip=True, source_offset_ms=0, bus_frame_id=1, bus_result_age_ms=None)
    clip = dict(source_id='member1', session_id='session', started_at_ms=1000, samples=[sample])
    monkeypatch.setattr(replay, 'review', dict(clips=[clip]))
    monkeypatch.setattr(replay, 'labels', dict(encounters=[dict(
        encounter_id='01-172', clip_no=1, track_ids=[7], ground_truth_number='172', boarding='approach')]))
    predictions = {(('member1', 'session'), 1, 1000): dict(routes=[dict(
        origin_track_id=7, track_id=7, route_number='172', state='matched_candidate', is_target=True)])}
    result = replay.evaluate(predictions)
    assert result['accepted'] == 1 and result['confirmed'] == 1
    assert result['encounters'][0]['accepted'] is None
    assert result['encounters'][0]['confirmed'] is None
