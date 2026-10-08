"""Read-only replay of archived model outputs, with isolated policy variants."""
import copy
import json
from pathlib import Path
import argparse
import hashlib
import re
from backend.bus.bus_runtime.target import exact_route

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'test-result/bus-review/quantitative/postprocess'
review = labels = historical = None
INPUT_PATHS = []

def initialize_inputs(review_path=None, labels_path=None, historical_path=None, output_dir=None):
    global OUT, review, labels, historical, INPUT_PATHS
    OUT = Path(output_dir or OUT).resolve()
    if OUT.is_relative_to((ROOT/'data').resolve()):
        raise ValueError('Experiment output must be outside archived data.')
    OUT.mkdir(parents=True, exist_ok=True)
    INPUT_PATHS = [Path(review_path or ROOT/'test-result/bus-review/review.json').resolve(),
                   Path(labels_path or ROOT/'test-result/bus-review/quantitative/annotations.json').resolve(),
                   Path(historical_path or ROOT/'test-result/bus-review/quantitative/report.json').resolve()]
    review, labels, historical = [json.loads(path.read_text()) for path in INPUT_PATHS]
    assert set(review['source_ids']) == {'member1', 'member3'}


def matcher_types(window=1.2, gap=.6, preserve=False):
    # Experiment-only copies: never mutate the production matcher modules.
    source=(ROOT/'backend/bus/bus_runtime/target.py').read_text()
    source, replacements = re.subn(r'window_s=\d+\.\d+', f'window_s={window}', source)
    assert replacements == 1 and source.count('> .6+1e-6') == 1
    source=source.replace('> .6+1e-6',f'> {gap}+1e-6')
    if preserve:source=source.replace("state['hits'].clear()\n        elif good", "pass\n        elif good")
    ns={};exec(compile(source,'experiment/target.py','exec'),ns)
    target=ns['TargetMatcher']
    source=(ROOT/'backend/bus/bus_runtime/observed.py').read_text()
    assert source.count('> .6 + 1e-6') == 1
    source=source.replace('from .target import TargetMatcher, exact_route, route_aliases, token_quality','')
    source, replacements = re.subn(r'timestamp - last_seen > [\d.]+:', f'timestamp - last_seen > {window}:', source)
    assert replacements == 1
    source=source.replace('> .6 + 1e-6', f'> {gap} + 1e-6')
    ns={k:ns[k] for k in ['exact_route','route_aliases','token_quality']};ns['TargetMatcher']=target
    exec(compile(source,'experiment/observed.py','exec'),ns)
    return target, ns['ObservedRouteMatcher']

def load_sessions():
    sessions={}
    for c in review['clips']:
        key=(c['source_id'],c['session_id'])
        if key in sessions:continue
        p=ROOT/'data/result-hub/sources'/key[0]/'sessions'/key[1]/'results.jsonl'
        records={}
        for ln,line in enumerate(p.open(),1):
            row=json.loads(line);bus=row.get('bus') or {};event=bus.get('event') or {}
            if bus.get('captured_at_ms') is None or not event or bus.get('frame_id') is None:continue
            records.setdefault((bus['frame_id'],bus['captured_at_ms']), dict(bus=bus,line=ln))
        sessions[key]=sorted(records.values(),key=lambda r:(r['bus']['captured_at_ms'],r['bus']['frame_id']))
    return sessions

def box_tuple(box):
    return tuple(box[k] for k in ['x1','y1','x2','y2'])

def exact(truth, text):
    return text==truth or truth in {'03','04','05'} and text=='서대문'+truth

def replay(sessions, window=1.2, gap=.6, preserve=False, linker_factory=None, overrides=None):
    Target,Observed=matcher_types(window,gap,preserve)
    predictions={}
    for key, records in sessions.items():
        target_number=object();target=None;other=None;linker=None
        previous=None
        for record in records:
            bus=record['bus'];event=bus['event'];t=bus['captured_at_ms']/1000
            route=event.get('target_route')
            if route!=target_number:
                target_number=route;target=Target(route,single_score=.98) if route else None
                other=Observed();linker=linker_factory() if linker_factory else None;previous=None
            ordered=previous is None or t>previous
            if ordered:previous=t
            buses=copy.deepcopy(event.get('buses',[]))
            if overrides:
                for bi,b in enumerate(buses):
                    replacement=overrides.get((key,bus['frame_id'],bus['captured_at_ms'],bi))
                    if replacement is not None:b['observations']=replacement
            origins=[b['track_id'] for b in buses]
            if linker is not None and ordered:linker.update(buses,t)
            routes=[];decisions=[]
            for bi,b in enumerate(buses):
                tid=b['track_id'];box=box_tuple(b['box']);obs=b['observations']
                complete=bool(ordered and not b.get('candidate_truncated'))
                decision=target.update(tid,t,obs,box,complete=complete) if target and ordered else {'state':'not_configured'}
                decisions.append(dict(origin_track_id=origins[bi],track_id=tid,**decision))
                if not ordered:continue
                if target and tid is not None and decision['state'] in {'recognized_single','matched_candidate'}:
                    supporting=[o for o in obs if o.get('eligible') and exact_route(o['text'],route) and min(o.get('token_scores') or [0])>=.9]
                    if supporting:routes.append(dict(track_id=tid,origin_track_id=origins[bi],route_number=route,state=decision['state'],is_target=True))
                for item in other.update(tid,t,obs,box,target=route,complete=complete):
                    routes.append({**item,'origin_track_id':origins[bi]})
            predictions[(key,bus['frame_id'],bus['captured_at_ms'])]=dict(routes=routes,decisions=decisions,buses=buses)
    return predictions


def replay_current_pipeline(sessions):
    """Check actual pipeline wiring using archived model outputs, without models."""
    import numpy as np
    from backend.bus.base import InferenceContext, ModelSpec
    from backend.bus.pipeline import BusPipeline

    class Detector:
        def sync(self): pass
        def reset(self): pass
        def buses(self, rgb): return copy.deepcopy(self.items)

    frame = np.zeros((960, 540, 3), dtype=np.uint8)
    predictions = {}
    for key, records in sessions.items():
        pipeline = BusPipeline(ModelSpec(id='archived', mode='bus', name='archive', version='replay', weights=None))
        detector = Detector()
        pipeline.detector = detector
        pipeline.recognizer = object()
        current_route = object()
        for record in records:
            bus = record['bus']
            event = bus['event']
            route = event.get('target_route')
            if route != current_route:
                pipeline.configure_target_route(route)
                pipeline.reset_session(key[1])
                current_route = route
            detector.items = [dict(track_id=b['track_id'], bus_score=b['confidence'],
                                   bus_box=tuple(round(b['box'][axis]*scale) for axis,scale in [('x1',540),('y1',960),('x2',540),('y2',960)]),
                                   candidate_count=b.get('candidate_count',0), eligible_candidate_count=b.get('eligible_candidate_count',0),
                                   candidate_truncated=b.get('candidate_truncated',False)) for b in event.get('buses',[])]
            observations = [{**item, 'bus_index':i} for i,b in enumerate(event.get('buses',[])) for item in b['observations']]
            pipeline._observations = lambda rgb,buses,items=observations: (copy.deepcopy(items), event.get('errors',[]))
            result = pipeline.infer(frame, InferenceContext(key[1],bus['frame_id'],bus['captured_at_ms']))['event']
            origins = {b['track_id']:b['detector_track_id'] for b in result['buses']}
            routes = [{**item, 'origin_track_id':origins[item['track_id']]} for item in result['recognized_routes']]
            predictions[(key,bus['frame_id'],bus['captured_at_ms'])] = dict(routes=routes,buses=result['buses'])
    return predictions

def evaluate(predictions):
    results=[];false_outputs=[];unknown_outputs=[]
    active=[a for a in labels['encounters'] if a['boarding']!='model_error']
    for a in active:
        c=review['clips'][a['clip_no']-1];key=(c['source_id'],c['session_id'])
        accepted=[];confirmed=[]
        for s in c['samples']:
            if not s['source_in_clip'] or s['source_offset_ms']/1000<a.get('start_s',0):continue
            p=predictions.get((key,s['bus_frame_id'],c['started_at_ms']+s['source_offset_ms']))
            if not p:continue
            for x in p['routes']:
                if x['origin_track_id'] not in a['track_ids'] or not exact(a['ground_truth_number'],x['route_number']):continue
                at=(s['source_offset_ms']+s['bus_result_age_ms'])/1000 if s['bus_result_age_ms'] is not None else None
                accepted.append(at)
                if x['state']=='matched_candidate':confirmed.append(at)
        known_accepted = [at for at in accepted if at is not None]
        known_confirmed = [at for at in confirmed if at is not None]
        results.append(dict(encounter_id=a['encounter_id'],accepted=min(known_accepted) if known_accepted else None,
                            confirmed=min(known_confirmed) if known_confirmed else None,
                            accepted_observed=bool(accepted), confirmed_observed=bool(confirmed)))
    for i,c in enumerate(review['clips'],1):
        key=(c['source_id'],c['session_id']);aa=[a for a in active if a['clip_no']==i]
        for s in c['samples']:
            if not s['source_in_clip']:continue
            p=predictions.get((key,s['bus_frame_id'],c['started_at_ms']+s['source_offset_ms']))
            if not p:continue
            for x in p['routes']:
                known=[a for a in aa if x['origin_track_id'] is not None and x['origin_track_id'] in a['track_ids'] and s['source_offset_ms']/1000>=a.get('start_s',0)]
                issue=dict(clip_no=i,source_s=s['source_offset_ms']/1000,**x)
                if known and not any(exact(a['ground_truth_number'],x['route_number']) for a in known):false_outputs.append(issue)
                elif not known and not any(exact(a['ground_truth_number'],x['route_number']) for a in aa):unknown_outputs.append(issue)
    return dict(accepted=sum(x['accepted_observed'] for x in results),confirmed=sum(x['confirmed_observed'] for x in results),
                false_outputs=false_outputs,unknown_outputs=unknown_outputs,encounters=results)

def compare(base,result):
    changes=[]
    for a,b in zip(base['encounters'],result['encounters']):
        assert a['encounter_id']==b['encounter_id']
        if a!=b:changes.append(dict(encounter_id=a['encounter_id'],baseline=a,variant=b))
    return changes

def main():
    parser=argparse.ArgumentParser(description='Compare isolated bus policies on unique archived OCR outputs, without training or changing weights.')
    parser.add_argument('--review',type=Path)
    parser.add_argument('--labels',type=Path)
    parser.add_argument('--historical-report',type=Path)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    initialize_inputs(args.review,args.labels,args.historical_report,args.output_dir)
    from backend.bus.bus_runtime.continuity import BusTrackContinuity
    sessions=load_sessions();results={};baseline=None
    variants=[('baseline',{}),('gap_only',{'gap':1.0}),('window_only',{'window':1.5}),('window2_only',{'window':2.0}),
              ('gap_window',{'gap':1.0,'window':1.5}),('gap_window2',{'gap':1.0,'window':2.0}),
              ('preserve_incomplete',{'preserve':True}),('tracking',{'linker_factory':BusTrackContinuity}),
              ('selected',{'window':2.0,'linker_factory':BusTrackContinuity})]
    for name,kw in variants:
        r=evaluate(replay(sessions,**kw));results[name]=r
        if baseline is None:baseline=r
        print(name,r['accepted'],r['confirmed'],'false',len(r['false_outputs']),'unknown',len(r['unknown_outputs']))
        print(json.dumps(compare(baseline,r),ensure_ascii=False))
    actual = evaluate(replay_current_pipeline(sessions))
    assert actual == results['selected'], 'Production pipeline differs from the selected experiment.'
    print('Production pipeline matches selected replay.')
    discrepancies=[]
    for item in baseline['encounters']:
        archived=next(x for x in historical['encounters'] if x['encounter_id']==item['encounter_id'])
        if item['accepted_observed']!=archived['accepted'] or item['confirmed_observed']!=archived['confirmed']:
            discrepancies.append(dict(encounter_id=item['encounter_id'],archived_accepted=archived['accepted'],archived_confirmed=archived['confirmed'],replayed=item))
    evidence_paths=[ROOT/'backend/bus/bus_runtime'/name for name in ['target.py','observed.py','continuity.py']]
    evidence_paths += INPUT_PATHS + [Path(__file__).resolve(), ROOT/'backend/bus/pipeline.py']
    evidence_paths += [ROOT/'data/result-hub/sources'/source/'sessions'/session/'results.jsonl' for source,session in sessions]
    output=dict(method='Read-only unique OCR snapshots ordered by original capture time, preserving full recorded session history. These are paired policy replays, not an exact reconstruction of missing worker executions or screen/speech timing.',
                archived_summary=historical['summary']['active'],baseline_discrepancies=discrepancies,variants=results,production_replay_verified=True,
                input_hashes={str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path):hashlib.sha256(path.read_bytes()).hexdigest() for path in evidence_paths})
    (OUT/'experiments.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print('Archived/replay discrepancies:',json.dumps(discrepancies,ensure_ascii=False))
    print('Saved:',OUT/'experiments.json')

if __name__=='__main__':
    main()
