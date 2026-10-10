"""B operating route-display evidence policy, ported from scripts/infer_target_video.py.

Source behavior is preserved; CRAFT fallback is disabled by the app runtime.
"""
from dataclasses import asdict
import math
import re
from PIL import Image
from .target import token_quality
from .route_evidence import crop_rejection, text_rejection, context_boxes, context_rejection
from .region_recovery import recovery_boxes, confirmation_box, is_extension, RECOVERY_POLICY

def overlap(a,b):
    x,y,z,w=a;u,v,s,t=b
    inter=max(0,min(z,s)-max(x,u))*max(0,min(w,t)-max(y,v))
    union=(z-x)*(w-y)+(s-u)*(t-v)-inter
    return inter/union if union else 0.


DUPLICATE_BUS_POLICY = {
    'min_bus_iou': .8,
    'min_region_iou': .8,
    'min_token_score': .98,
    'scope': 'route-display detector only; two OCR reads of the same pixels',
}

OTHER_BUS_MAX_ROI_FRACTION = .05


def _pixel_box(value):
    """Validate the pixel xyxy boxes supplied by the inference pipeline."""
    try:
        box = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if (len(box) != 4 or not all(math.isfinite(v) for v in box)
            or box[2] <= box[0] or box[3] <= box[1]):
        return None
    return box


def gate_other_bus_overlap(current, buses):
    """Veto risky reads after OCR/context/recovery, preserving raw evidence.

    Each other bus is checked against the number ROI area, not box IoU.
    An independently confirmed duplicate peer is the same physical bus;
    only that recorded peer is exempt, while third buses remain checked.
    This does not change candidate budgets or erase prior matcher evidence.
    """
    for item in current:
        if not item.get('eligible'):
            continue
        roi = _pixel_box(item.get('text_box'))
        reason = None
        if roi is None:
            reason = 'visibility_geometry_unavailable'
        else:
            a, b, c, d = roi
            area = (c-a)*(d-b)
            own_index = item.get('bus_index')
            peer_index = (item.get('duplicate_bus_partner_index')
                          if item.get('duplicate_bus_recovered') else None)
            for index, bus in enumerate(buses):
                other = _pixel_box(bus.get('bus_box'))
                if own_index is not None:
                    own = index == own_index
                elif item.get('track_id') is not None:
                    own = item['track_id'] == bus.get('track_id')
                else:
                    own = other is not None and other == _pixel_box(item.get('bus_box'))
                if own or index == peer_index or other is None:
                    continue
                x, y, z, w = other
                intersection = max(0., min(c, z)-max(a, x)) * max(0., min(d, w)-max(b, y))
                if intersection/area >= OTHER_BUS_MAX_ROI_FRACTION - 1e-9:
                    reason = 'other_bus_bbox_overlap_risk'
                    break
        if reason:
            item['eligible'] = False
            item['visibility_rejection_reason'] = reason


FALLBACK_EVIDENCE_POLICY = {
    'min_token_score': .98,
    'route_text_pattern': r'(?:[가-힣]+|[A-Za-z]+)?[0-9]{2,4}[A-Za-z]?(?:-[0-9]+)?',
    'scope': 'CRAFT regions used only when the fixed B detector found no region',
}
ROUTE_TEXT_PATTERN = re.compile(FALLBACK_EVIDENCE_POLICY['route_text_pattern'])


def gate_fallback_evidence(current):
    """Keep only complete, high-quality route-shaped fallback reads as evidence.

    The fixed B detector's observations and all existing crop/context rejections
    remain untouched. CRAFT still runs on the same pixels; raw OCR stays logged.
    """
    for item in current:
        if item.get('region_source') != 'craft' or not item['eligible']:
            continue
        if not ROUTE_TEXT_PATTERN.fullmatch(item['text']):
            item['eligible'] = False
            item['rejection_reason'] = 'fallback_nonroute_text'
        elif token_quality(item['token_scores']) < FALLBACK_EVIDENCE_POLICY['min_token_score']:
            item['eligible'] = False
            item['rejection_reason'] = 'fallback_low_token_score'


def recover_duplicate_bus_candidate(rgb, current, buses):
    """Keep one route-region read when duplicate bus tracks veto each other.

    The CRAFT path contains general text, so its ambiguous-bus veto stays intact.
    A pair must independently read the same pixels and satisfy every other crop
    filter. A third bus containing either region keeps ownership ambiguous.
    """
    def center_in(box, outer):
        a, b, c, d = box
        x, y, z, w = outer
        return x <= (a+c)/2 <= z and y <= (b+d)/2 <= w

    def otherwise_valid(p):
        if p['rejection_reason'] != 'small_clipped_or_ambiguous_bus':
            return False
        # CRAFT detects arbitrary text, so duplicate-track recovery is only
        # permitted for regions proposed by the route-display detector.
        if p.get('region_source','route_display') != 'route_display':
            return False
        if not ROUTE_TEXT_PATTERN.fullmatch(p['text']):
            return False
        if token_quality(p['token_scores']) < DUPLICATE_BUS_POLICY['min_token_score']:
            return False
        a, b, c, d = p['text_box']
        h, w = rgb.shape[:2]
        if c-a < 10 or d-b < 6 or a <= 0 or b <= 0 or c >= w-1 or d >= h-1:
            return False
        if crop_rejection(rgb, p['text_box'], p['bus_box'], p['search_box']):
            return False
        return not text_rejection(p['text'], p['text_box'])

    candidates = [(i, p) for i, p in enumerate(current) if otherwise_valid(p)]
    used = set()
    for i, left in candidates:
        if i in used:
            continue
        for j, right in candidates:
            if j <= i or j in used or left['bus_index'] == right['bus_index']:
                continue
            li, ri = left['bus_index'], right['bus_index']
            if (left['text'] != right['text']
                    or overlap(buses[li]['bus_box'], buses[ri]['bus_box']) < DUPLICATE_BUS_POLICY['min_bus_iou']
                    or overlap(left['text_box'], right['text_box']) < DUPLICATE_BUS_POLICY['min_region_iou']
                    or not center_in(left['text_box'], buses[ri]['bus_box'])
                    or not center_in(right['text_box'], buses[li]['bus_box'])):
                continue
            if any(center_in(left['text_box'], bus['bus_box']) or center_in(right['text_box'], bus['bus_box'])
                   for k, bus in enumerate(buses) if k not in {li, ri}):
                continue
            winner_index, winner = max(((i, left), (j, right)),
                                       key=lambda pair: (token_quality(pair[1]['token_scores']),
                                                         buses[pair[1]['bus_index']].get('bus_score', 0), -pair[0]))
            winner['eligible'] = True
            winner['rejection_reason'] = ''
            winner['duplicate_bus_recovered'] = True
            winner['duplicate_bus_partner_index'] = ri if winner_index == i else li
            used.update((i, j))
            break


def eligible_box(box,bus_index,buses,shape):
    a,b,c,d=box;h,w=shape[:2]
    if c-a<10 or d-b<6 or a<=0 or b<=0 or c>=w-1 or d>=h-1:return False
    cx,cy=(a+c)/2,(b+d)/2
    for j,other in enumerate(buses):
        x,y,z,t=other['bus_box']
        if j!=bus_index and x<=cx<=z and y<=cy<=t:return False
    return True


def select_candidates(rgb, items, bus_index, buses, max_candidates):
    """검사에서 제외된 crop이 온전한 crop이나 OCR 예산을 밀어내지 않게 한다."""
    checked=[]
    for item in items:
        rejection=crop_rejection(rgb,item['text_box'],buses[bus_index]['bus_box'],item['search_box'])
        if not eligible_box(item['text_box'],bus_index,buses,rgb.shape):
            rejection=rejection or 'small_clipped_or_ambiguous_bus'
        item={**item,'eligible':not rejection,'rejection_reason':rejection}
        duplicates=[i for i,p in enumerate(checked)
                    if p['search_zone']!=item['search_zone'] and overlap(p['text_box'],item['text_box'])>.5]
        if duplicates:
            # 기존 상단 crop이 잘렸고 차체 탐색에 온전한 crop이 있으면 교체한다.
            # 양쪽 모두 유효하면 기존 우선순위를 유지한다. 목표 문자열은 참조하지 않는다.
            if item['eligible'] and all(not checked[i]['eligible'] for i in duplicates):
                for i in reversed(duplicates):checked.pop(i)
                checked.append(item)
        else:checked.append(item)
    checked.sort(key=lambda p:(p['eligible'],(p['text_box'][2]-p['text_box'][0])*(p['text_box'][3]-p['text_box'][1])),reverse=True)
    valid_count=sum(p['eligible'] for p in checked)
    # 아직 OCR하지 않은 유효 후보가 있으면 여전히 incomplete로 보류한다.
    return checked[:max_candidates],len(checked),valid_count,valid_count>max_candidates


def recover_regions(rgb,current,buses,recognizer):
    """Require matching OCR of two larger crops before replacing a partial source."""
    jobs=[]
    for j,bus in enumerate(buses):
        sources=[p for p in current if p['bus_index']==j and recovery_boxes(p,rgb.shape)]
        sources.sort(key=lambda p:min(p['token_scores']),reverse=True)
        for p in sources[:RECOVERY_POLICY['max_sources_per_bus']]:
            for side,box in recovery_boxes(p,rgb.shape):
                jobs.append((p,side,box))
    confirmations=[]
    for start in range(0,len(jobs),16):
        chunk=jobs[start:start+16]
        crops=[Image.fromarray(rgb[b:d,a:c]) for _,_,(a,b,c,d) in chunk]
        for (source,side,box),pred in zip(chunk,recognizer.predict_batch(crops)):
            check={'side':side,'box':box,**asdict(pred),'accepted':False}
            source.setdefault('recovery_checks',[]).append(check)
            if not is_extension(source['text'],pred.text,pred.token_scores,side):continue
            confirm=confirmation_box(box,side,source['text_box'],source['search_box'])
            if confirm is not None:confirmations.append((source,side,box,pred,check,confirm))
    accepted=[]
    for start in range(0,len(confirmations),16):
        chunk=confirmations[start:start+16]
        crops=[Image.fromarray(rgb[b:d,a:c]) for *_,(a,b,c,d) in chunk]
        for (source,side,box,pred,check,confirm),second in zip(chunk,recognizer.predict_batch(crops)):
            check['confirmation']={'box':confirm,**asdict(second)}
            if second.text!=pred.text or not is_extension(source['text'],second.text,second.token_scores,side):continue
            reason=crop_rejection(rgb,box,source['bus_box'],source['search_box'])
            if not eligible_box(box,source['bus_index'],buses,rgb.shape):reason=reason or 'small_clipped_or_ambiguous_bus'
            a,b,c,d=box
            # A complete, independently re-read oblique word can be narrower than
            # the unverified single-crop density limit. Keep a finite width floor.
            if (c-a)/max(1,d-b)/len(pred.text)<RECOVERY_POLICY['min_extended_aspect_per_character']:
                reason=reason or 'implausible_character_density'
            item={**source,**asdict(pred),'text_box':box,'eligible':not reason,
                  'rejection_reason':reason,'context_checks':[],'recovery_checks':[],
                  'recovered_from':{'box':source['text_box'],'text':source['text'],'side':side}}
            # Keep registration/phone rejection active on the expanded region too.
            for context in context_boxes(item,rgb.shape):
                a,b,c,d=context;cp=recognizer.predict_batch([Image.fromarray(rgb[b:d,a:c])])[0]
                rejection=context_rejection(item['text'],cp.text,cp.token_scores)
                item['context_checks'].append({'box':context,**asdict(cp),'rejection_reason':rejection})
                if rejection:item['eligible']=False;item['rejection_reason']=rejection
            if item['eligible']:
                check['accepted']=True
                source['eligible']=False;source['rejection_reason']='superseded_by_full_region'
                accepted.append(item)
    current.extend(accepted)
