"""Confirm visible non-target routes without completing missing LED digits."""
from .target import TargetMatcher, exact_route, route_aliases, token_quality


def _fragment(text, route):
    """Even one remaining digit or an interior LED dropout is not another route."""
    for full in route_aliases(route):
        if len(text) >= len(full):
            continue
        letters = iter(full)
        if all(character in letters for character in text):
            return True
    return False


class ObservedRouteMatcher:
    """Require repeated, unambiguous readings on the same current bus track."""

    def __init__(self):
        self.candidates = {}
        self.tracks = {}

    def update(self, track_id, timestamp, observations, bus_box, *, target, complete):
        # Bound history to the same short evidence window as target recognition.
        for key, (_, last_seen) in list(self.candidates.items()):
            if timestamp - last_seen > 1.2:
                del self.candidates[key]
        for track, context in list(self.tracks.items()):
            if timestamp - context['last'] > .6 + 1e-6:
                del self.tracks[track]
        if track_id is None:
            return []
        context = self.tracks.get(track_id)
        if context and bus_box and context['box']:
            a, b, c, d = bus_box
            x, y, z, w = context['box']
            intersection = max(0, min(c, z) - max(a, x)) * max(0, min(d, w) - max(b, y))
            union = (c - a) * (d - b) + (z - x) * (w - y) - intersection
            if union <= 0 or intersection / union < .1:
                context = None
        if context is None:
            context = {'routes': {}, 'last': timestamp, 'box': bus_box}
        context.update(last=timestamp, box=bus_box)
        self.tracks[track_id] = context
        routes = set()
        for observation in observations:
            text = observation['text']
            if not observation.get('eligible') or token_quality(observation.get('token_scores')) < .9:
                continue
            try:
                route_aliases(text)
            except ValueError:
                continue
            context['routes'][text] = timestamp
            # A partial target such as 701/11 must not become another spoken route.
            if target and (exact_route(text, target) or _fragment(text, target)):
                continue
            routes.add(text)
        # Keep complete-number context while this physical track remains visible.
        # A temporary loss of digits must not turn a previously seen 1711 into 171.
        while len(context['routes']) > 64:
            del context['routes'][min(context['routes'], key=context['routes'].get)]
        known = {route for (track, route) in self.candidates if track == track_id}
        confirmed = []
        for route in sorted(routes | known):
            key = (track_id, route)
            matcher, last_seen = self.candidates.get(key, (TargetMatcher(route), timestamp))
            # Any other confident full number on this vehicle blocks an announcement,
            # including a longer number that contains the short reading.
            ambiguous = any(_fragment(route, previous) for previous in context['routes']) or any(
                item.get('eligible') and token_quality(item.get('token_scores')) >= .9
                and not exact_route(item['text'], route)
                for item in observations
            )
            decision = matcher.update(track_id, timestamp, observations, bus_box,
                                      complete=complete and not ambiguous)
            self.candidates[key] = (matcher, timestamp if route in routes else last_seen)
            if route in routes and decision['state'] == 'matched_candidate':
                confirmed.append({
                    'track_id': track_id, 'route_number': route,
                    'state': decision['state'], 'is_target': False,
                    'token_score': max(token_quality(item.get('token_scores'))
                                       for item in observations
                                       if item.get('eligible') and exact_route(item['text'], route)),
                })
        return confirmed
