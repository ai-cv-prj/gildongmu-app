"""Bus number inference using the evaluated B route-display evidence path.

The selected model is the fixed B route-display YOLO weight. Bus YOLO11n,
PARSeq, and the pinned PARSeq source are configured by configs/bus.yaml.
GPS and arrival data do not enter this pipeline.
"""
from __future__ import annotations

from dataclasses import asdict
import logging
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .base import InferenceContext, ModelSpec, normalize_box
from .bus_runtime.evidence import (select_candidates, recover_duplicate_bus_candidate,
                                   recover_regions)
from .bus_runtime.route_evidence import context_boxes, context_rejection, text_rejection
from .bus_runtime.target import TargetMatcher, exact_route, token_quality
from .bus_runtime.observed import ObservedRouteMatcher
from .bus_runtime.led_diagnostics import led_row_diagnostics

log = logging.getLogger(__name__)

CLASS_NAMES = {0: 'bus', 1: 'route_number'}
MAX_CANDIDATES_PER_BUS = 24
OCR_BATCH_SIZE = 16


class BusPipeline:
    mode = 'bus'

    def __init__(self, spec: ModelSpec, asset_paths: dict | None = None) -> None:
        self.spec = spec
        self.weights = spec.weights
        self.asset_paths = asset_paths or {}
        self.detector = None
        self.recognizer = None
        self.target_route: str | None = None
        self.matcher: TargetMatcher | None = None
        self.session_id: str | None = None
        self.last_capture_ms: int | None = None
        self.observed_matcher = ObservedRouteMatcher()

    def load(self) -> None:
        if self.weights is None or not self.weights.is_file():
            raise FileNotFoundError('B route-display weights are missing')
        aux = Path(self.weights).parent / 'aux'
        bus_weights = Path(self.asset_paths.get('bus_detector', aux / 'yolo11n.pt'))
        parseq_weights = Path(self.asset_paths.get('parseq_weights', aux / 'parseq-bb5792a6.pt'))
        parseq_source = Path(self.asset_paths.get('parseq_source', aux / 'parseq-src'))
        for path in (bus_weights, parseq_weights, parseq_source / 'hubconf.py'):
            if not path.is_file():
                raise FileNotFoundError(f'Bus auxiliary model is missing: {path}')
        import torch
        from .bus_runtime.models import RouteDetector
        from .bus_runtime.recognizer import PARSeqRecognizer

        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.detector = RouteDetector(Path(self.weights), bus_weights, device,
                                      Path(tempfile.gettempdir()) / 'gildongmu-app-ultralytics')
        self.recognizer = PARSeqRecognizer(parseq_source, parseq_weights, device)

    def configure_target_route(self, route: str | None) -> None:
        """Set the user-entered route for the next bus session, independent of GPS."""
        normalized = route.strip() if route is not None else None
        if normalized:
            TargetMatcher(normalized, single_score=.98)  # validate at session start
        self.target_route = normalized or None

    def reset_session(self, session_id: str) -> None:
        self.session_id = session_id
        self.last_capture_ms = None
        self.observed_matcher = ObservedRouteMatcher()
        self.matcher = (TargetMatcher(self.target_route, single_score=.98)
                        if self.target_route else None)
        if self.detector is not None:
            self.detector.reset()

    def close_session(self, session_id: str) -> None:
        if self.session_id != session_id:
            return
        if self.detector is not None:
            self.detector.reset()
        self.session_id = None
        self.target_route = None
        self.matcher = None
        self.last_capture_ms = None
        self.observed_matcher = ObservedRouteMatcher()

    @staticmethod
    def _led_diagnostics(rgb: np.ndarray, box) -> dict | None:
        # Logged for field analysis only; a failure here must not affect OCR.
        try:
            return led_row_diagnostics(rgb[box[1]:box[3], box[0]:box[2]])
        except Exception:
            log.exception('LED diagnostics failed')
            return None

    def _observations(self, rgb: np.ndarray, buses: list[dict]) -> tuple[list[dict], list[str]]:
        pending: list[dict] = []
        errors: list[str] = []
        for bus_index, bus in enumerate(buses):
            bus['candidate_count'] = 0
            bus['eligible_candidate_count'] = 0
            bus['candidate_truncated'] = False
            try:
                # B searches the whole bus once. The second body pass is empty.
                search_box, boxes = self.detector.text_regions(rgb, bus['bus_box'])
                items = [{'text_box': box, 'search_zone': 'upper',
                          'search_box': search_box, 'region_source': 'route_display'}
                         for box in boxes]
                selected, count, valid_count, truncated = select_candidates(
                    rgb, items, bus_index, buses, MAX_CANDIDATES_PER_BUS)
                bus['candidate_count'] = count
                bus['eligible_candidate_count'] = valid_count
                bus['candidate_truncated'] = truncated
                for candidate_index, item in enumerate(selected):
                    pending.append({'track_id': bus['track_id'], 'bus_index': bus_index,
                                    'candidate_index': candidate_index,
                                    'bus_box': bus['bus_box'], **item})
            except Exception as exc:  # bus box remains available if route detection fails
                log.exception('route-display detection failed for bus %s', bus_index)
                bus['candidate_truncated'] = True
                errors.append(f'region_detection_failed:{bus_index}:{type(exc).__name__}')
        current: list[dict] = []
        try:
            for start in range(0, len(pending), OCR_BATCH_SIZE):
                chunk = pending[start:start+OCR_BATCH_SIZE]
                crops = [Image.fromarray(rgb[p['text_box'][1]:p['text_box'][3],
                                            p['text_box'][0]:p['text_box'][2]]) for p in chunk]
                for item, pred in zip(chunk, self.recognizer.predict_batch(crops)):
                    rejection = item['rejection_reason'] or text_rejection(pred.text, item['text_box'])
                    current.append({**item, **asdict(pred), 'eligible': not rejection,
                                    'rejection_reason': rejection,
                                    'duplicate_bus_recovered': False,
                                    'led_diagnostics': self._led_diagnostics(rgb, item['text_box'])})
            recover_duplicate_bus_candidate(rgb, current, buses)
            contexts = []
            for item in current:
                item['context_checks'] = []
                for box in context_boxes(item, rgb.shape):
                    contexts.append((item, box))
            for start in range(0, len(contexts), OCR_BATCH_SIZE):
                chunk = contexts[start:start+OCR_BATCH_SIZE]
                crops = [Image.fromarray(rgb[b:d, a:c]) for _, (a, b, c, d) in chunk]
                for (item, box), pred in zip(chunk, self.recognizer.predict_batch(crops)):
                    rejection = context_rejection(item['text'], pred.text, pred.token_scores)
                    item['context_checks'].append({'box': box, **asdict(pred),
                                                   'rejection_reason': rejection})
                    if rejection:
                        item['eligible'] = False
                        item['rejection_reason'] = rejection
            recover_regions(rgb, current, buses, self.recognizer)
        except Exception as exc:  # OCR must never erase valid bus detections
            log.exception('bus OCR failed')
            errors.append(f'ocr_failed:{type(exc).__name__}')
            for bus in buses:
                bus['candidate_truncated'] = True
            for item in current:
                item['eligible'] = False
                item['rejection_reason'] = 'ocr_failed'
        return current, errors

    def infer(self, frame_bgr: np.ndarray, context: InferenceContext) -> dict[str, Any]:
        if self.detector is None or self.recognizer is None:
            raise RuntimeError('BusPipeline.load() must complete before inference')
        height, width = frame_bgr.shape[:2]
        rgb = frame_bgr[:, :, ::-1].copy()
        self.detector.sync()
        buses = self.detector.buses(rgb)
        self.detector.sync()
        current, errors = self._observations(rgb, buses)
        self.detector.sync()
        captured_at_ms = int(context.captured_at_ms)
        ordered = self.last_capture_ms is None or captured_at_ms > self.last_capture_ms
        if ordered:
            self.last_capture_ms = captured_at_ms
        timestamp_s = captured_at_ms / 1000.0
        detections: list[dict] = []
        event_buses: list[dict] = []
        matches: list[dict] = []
        recognized_routes: list[dict] = []
        for bus_index, bus in enumerate(buses):
            track_id = bus['track_id']
            bus_box = bus['bus_box']
            observations = [p for p in current if p['bus_index'] == bus_index]
            complete = bool(ordered and not bus['candidate_truncated'])
            if self.matcher is None:
                decision = {'state': 'not_configured', 'reason': 'target_route_missing',
                            'support_samples': 0, 'conflicts': []}
            elif not ordered:
                decision = {'state': 'hold', 'reason': 'nonmonotonic_capture_time',
                            'support_samples': 0, 'conflicts': []}
            else:
                decision = self.matcher.update(track_id, timestamp_s, observations,
                                               bus_box, complete=complete)
            bus_record = {'track_id': track_id,
                          'box': normalize_box(*bus_box, width, height),
                          'confidence': float(bus['bus_score']),
                          'candidate_count': bus['candidate_count'],
                          'eligible_candidate_count': bus['eligible_candidate_count'],
                          'candidate_truncated': bus['candidate_truncated'],
                          'observations': [], 'decision': decision}
            for item in observations:
                quality = token_quality(item['token_scores'])
                observed = {**item, 'token_score': quality,
                            'box': normalize_box(*item['text_box'], width, height)}
                bus_record['observations'].append(observed)
                if item['eligible']:
                    detections.append({'class_id': 1, 'class_name': 'route_number',
                                       'confidence': quality,
                                       'box': observed['box'], 'track_id': track_id,
                                       'extra': {'text': item['text'], 'token_score': quality,
                                                 'score_is_calibrated_probability': False}})
            detections.append({'class_id': 0, 'class_name': 'bus',
                               'confidence': float(bus['bus_score']),
                               'box': bus_record['box'], 'track_id': track_id,
                               'extra': {'route_number': next((p['text'] for p in observations
                                                               if p['eligible']), None),
                                         'target_state': decision['state']}})
            event_buses.append(bus_record)
            if ordered:
                recognized_routes.extend(self.observed_matcher.update(
                    track_id, timestamp_s, observations, bus_box,
                    target=self.target_route, complete=complete))
            if (self.target_route and track_id is not None and
                    decision['state'] in {'recognized_single', 'matched_candidate'}):
                supporting = [p for p in observations if p['eligible']
                              and exact_route(p['text'], self.target_route)
                              and token_quality(p['token_scores']) >= .9]
                if supporting:
                    matches.append({'track_id': track_id, 'route_number': self.target_route,
                                    'state': decision['state'],
                                    'token_score': max(token_quality(p['token_scores'])
                                                       for p in supporting)})
        recognized_routes = [{**match, 'is_target': True} for match in matches] + recognized_routes
        event = {'type': 'bus_detection', 'target_route': self.target_route,
                 'buses': event_buses, 'matches': matches,
                 'recognized_routes': recognized_routes,
                 'bus_number': matches[0]['route_number'] if len(matches) == 1 else None,
                 'is_target': len(matches) == 1,
                 'errors': errors}
        return {'detections': detections, 'event': event}
