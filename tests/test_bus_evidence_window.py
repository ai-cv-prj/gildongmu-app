"""Sparse correct reads accumulate while the same vehicle remains observed."""
import pytest

from backend.bus.bus_runtime.target import TargetMatcher
from backend.bus.bus_runtime.observed import ObservedRouteMatcher


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
