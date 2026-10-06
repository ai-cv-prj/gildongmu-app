"""목표 노선과 영상 근거 비교. 누락 숫자 추정/치환을 하지 않는다."""
from collections import deque
import math
import re


def route_aliases(route):
    if not re.fullmatch(r'(?:[가-힣]+|[A-Za-z]+)?[0-9]+[A-Za-z]?(?:-[0-9]+)?', route):
        raise ValueError(f'지원하지 않는 목표 노선: {route!r}')
    # 지역 접두어만 생략 허용. 영문/선행 0/하이픈 보존.
    return {route, re.sub(r'^[가-힣]+', '', route)}


def exact_route(text, target):
    return text in route_aliases(target)


def token_quality(scores):
    return min(scores) if scores and all(math.isfinite(s) and 0 <= s <= 1 for s in scores) else 0.


def partial_route(text, target):
    for key in route_aliases(target):
        if 2 <= len(text) < len(key) and (key.startswith(text) or key.endswith(text)):
            return True
        if len(text) == len(key) and '?' in text and sum(c != '?' for c in text) >= 2:
            if all(a == '?' or a == b for a,b in zip(text,key)):
                return True
    return False


def competing_route(text, target, alternatives=()):
    if exact_route(text,target): return False
    if any(text in route_aliases(r) for r in alternatives): return True
    key=re.sub(r'^[가-힣]+','',target)
    # API 후보가 없는 실험에서도 한 글자 차이와 더 긴 경쟁 번호를 감시한다.
    # A/B는 이 실험의 지원 접미사다. 전국 노선 형식에 대한 주장이 아니다.
    # 명시적인 target/alternatives는 위의 alias 비교가 우선한다.
    valid = re.fullmatch(r'(?:M|N)?[0-9]+[AB]?(?:-[0-9]+)?', text)
    if not valid:return False
    # 같은 버스에서 온전한 긴 번호를 본 뒤 일부 숫자만 보이면 짧은 목표의
    # 독립적인 확증으로 사용하지 않는다. 짧은 OCR만 있을 때는 정상 판정을 유지한다.
    if len(text)>len(key) and (text.startswith(key) or text.endswith(key)):
        return True
    return len(text)==len(key) and sum(a!=b for a,b in zip(text,key))==1


class TargetMatcher:
    """실험용 일치 후보. 실제 탑승 확정기/보정된 확률이 아니다."""
    def __init__(self, target, alternatives=(), window_s=1.2, min_samples=3, min_span_s=.4, single_score=None):
        route_aliases(target)
        for route in alternatives: route_aliases(route)
        if any(route != target and route_aliases(target) & route_aliases(route) for route in alternatives):
            raise ValueError('후보 노선의 표시 번호가 겹칩니다. 현재 정류장에서 유일해야 합니다.')
        self.target, self.alternatives = target, tuple(alternatives)
        self.window_s,self.min_samples,self.min_span_s=window_s,min_samples,min_span_s
        if single_score is not None and (not math.isfinite(single_score) or not .9 <= single_score <= 1):
            raise ValueError('단일 관측 점수는 0.9~1 범위여야 합니다.')
        self.single_score=single_score
        self.tracks={}

    def update(self, track_id, timestamp, observations, bus_box=None, complete=True):
        if track_id is None:
            return {'state':'untracked','reason':'no_track_id','support_samples':0,'conflicts':[]}
        state=self.tracks.setdefault(track_id,{'hits':deque(),'last':None,'rivals':{},'box':None})
        if state['last'] is not None and timestamp < state['last']:
            raise ValueError('시각은 단조 증가해야 합니다.')
        jump=False
        if bus_box and state['box']:
            a,b,c,d=bus_box;x,y,z,w=state['box']
            inter=max(0,min(c,z)-max(a,x))*max(0,min(d,w)-max(b,y))
            union=(c-a)*(d-b)+(z-x)*(w-y)-inter
            jump=union<=0 or inter/union<.1
        if jump or (state['last'] is not None and timestamp-state['last'] > .6+1e-6):
            state['hits'].clear()
            state['rivals'].clear()
        state['last'],state['box']=timestamp,bus_box
        while state['hits'] and timestamp-state['hits'][0][0] > self.window_s+1e-6:
            state['hits'].popleft()
        for text, samples in list(state['rivals'].items()):
            while samples and timestamp-samples[0][0] > self.window_s+1e-6:
                samples.popleft()
            if not samples:del state['rivals'][text]
        good=False;partial=False;ambiguous=False;good_scores=None;single_good=False
        key=re.sub(r'^[가-힣]+','',self.target)
        for obs in observations:
            text=obs['text'];q=token_quality(obs.get('token_scores',[]))
            if not obs.get('eligible',True):
                ambiguous |= exact_route(text,self.target)
                continue
            # 암묵적으로 찾은 긴 번호는 한 번의 약한 OCR 때문에 온전한 목표
            # 한 프레임을 잃지 않도록 .9 이상만 경쟁 근거로 삼는다.
            explicit=any(text in route_aliases(r) for r in self.alternatives)
            rival_score=.9 if len(text)>len(key) and not explicit else .75
            if competing_route(text,self.target,self.alternatives) and q>=rival_score:
                samples=state['rivals'].setdefault(text,deque())
                if samples and samples[-1][0]==timestamp:
                    samples[-1]=(timestamp,max(q,samples[-1][1]))
                else:samples.append((timestamp,q))
            if exact_route(text,self.target):
                good |= q>=.9
                single_good |= self.single_score is not None and q>=self.single_score
                ambiguous |= q<.9
                if q>=.9 and (good_scores is None or q>min(good_scores)):
                    # 한글 접두어가 있는 출력은 숫자 표시 부분에 맞춰 점수를 정렬한다.
                    offset=len(text)-len(re.sub(r'^[가-힣]+','',text))
                    good_scores=obs['token_scores'][offset:]
            partial |= partial_route(text,self.target)
        if not complete:
            state['hits'].clear()
        elif good and (not state['hits'] or timestamp>state['hits'][-1][0]+1e-6):
            state['hits'].append((timestamp,good_scores))
        enough=good and len(state['hits'])>=self.min_samples and timestamp-state['hits'][0][0]>=self.min_span_s-1e-6
        blocked=[]
        for rival,samples in state['rivals'].items():
            # 높은 점수의 경쟁 번호 또는 서로 다른 프레임의 반복 경쟁은 보류.
            strong=max(q for _,q in samples)>=.9 or len(samples)>=2
            differing=[i for i,(a,b) in enumerate(zip(key,rival)) if a!=b]
            # 낮은 점수의 단발 오독은 전체 목표 번호 3회와 구별 문자 0.97 이상으로만 해소.
            decisive=sum(bool(differing) and all(i<len(scores)-1 and scores[i]>=.97 for i in differing)
                         for _,scores in state['hits'])>=self.min_samples
            simultaneous=samples[-1][0]==timestamp
            if strong or simultaneous or not (enough and decisive):blocked.append(rival)
        if blocked:
            label,reason='conflict','different_full_number_on_same_track'
        elif not complete or jump:
            label,reason='hold','incomplete_scan_or_track_jump'
        elif enough:
            label,reason='matched_candidate','repeated_full_number_current_frame'
        elif single_good:
            # 현재 프레임의 온전한 번호만 인정한다. 미래 프레임으로 소급 확정하지 않는다.
            # 추적·탐색 완전성·경쟁 번호 검사는 위에서 동일하게 적용한다.
            label,reason='recognized_single','high_score_full_number_current_frame'
        elif good:
            label,reason='pending','full_number_needs_repetition'
        elif partial:
            label,reason='partial','missing_digits_never_completed'
        elif ambiguous:
            label,reason='hold','low_score_or_ambiguous_assignment'
        else:
            label,reason='unverified','no_current_full_number'
        return {'state':label,'reason':reason,'support_samples':len(state['hits']),
                'conflicts':sorted(blocked)}
