"""Bounded route-display search for a nearby bus with no primary candidates."""
import re

from .target import token_quality


ZOOM_POLICY = {
    'max_buses_per_frame': 1,
    'max_candidates_per_bus': 4,
    'min_bus_height_fraction': .5,
    'min_bus_area_fraction': .2,
    'min_token_score': .98,
}
ZOOM_ROUTE_PATTERN = re.compile(r'(?:[가-힣]+|[A-Za-z]+)?[0-9]{2,4}[A-Za-z]?(?:-[0-9]+)?')


def zoom_bus_index(buses, shape):
    """Prefer the largest nearby vehicle; never retry a failed/incomplete scan."""
    height, width = shape[:2]
    candidates = []
    for index, bus in enumerate(buses):
        if bus['candidate_truncated'] or bus['region_search']['primary_candidates']:
            continue
        x, y, z, bottom = bus['bus_box']
        area = max(0, z-x) * max(0, bottom-y)
        if (bottom-y >= height * ZOOM_POLICY['min_bus_height_fraction']
                and area >= height * width * ZOOM_POLICY['min_bus_area_fraction']):
            candidates.append((area, -index))
    return -max(candidates)[1] if candidates else None


def zoom_rejection(observation):
    """Extra search cannot introduce arbitrary text or weak route readings."""
    if observation.get('region_source') != 'route_display_zoom':
        return ''
    if not ZOOM_ROUTE_PATTERN.fullmatch(observation['text']):
        return 'zoom_nonroute_text'
    scores = observation.get('token_scores', [])
    if len(scores) < len(observation['text']) + 1 or token_quality(scores) < ZOOM_POLICY['min_token_score']:
        return 'zoom_low_token_score'
    return ''
