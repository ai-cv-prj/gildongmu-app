"""Shared classification persists independently of uploaded artifacts and admin mode."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from result_hub.app import create_app
from test_hub_admin import (settings, client, seed_session, upload, remove_session,
                            SID, VIEWER, TOKENS, ADMIN_HEADERS)

DETAIL = f"/api/sessions/member1/{SID}"
URL = DETAIL + "/clips/clip_001/categories"
HEADERS = {"X-Hub-Request": "categories"}


def save(client, categories, revision=0, **kwargs):
    return client.put(URL, json={"categories": categories, "revision": revision},
                      auth=VIEWER, headers=HEADERS, **kwargs)


def listed(client, **params):
    return client.get('/api/sessions', auth=VIEWER, params=params).json()['sessions']


def test_team_can_classify_without_admin_and_filter_in_combination(settings):
    with TestClient(create_app(replace(settings, admin_password=""))) as client:
        seed_session(client)
        seed_session(client, 'member2')
        assert len(listed(client, category='unclassified')) == 2
        result = save(client, ['bus', 'obstacle', 'bus'])
        assert result.status_code == 200, result.text
        assert result.json()['item'] == {'category_key': 'clip_001', 'categories': ['obstacle', 'bus'], 'category_revision': 1}
        for category in ('bus', 'obstacle'):
            assert [row['source_id'] for row in listed(client, category=category)] == ['member1']
        assert listed(client, category='traffic_light') == []
        assert listed(client, category='bus', source_id='member2') == []
        assert listed(client, category='bus', q='absent') == []
        assert len(listed(client, category='bus', q='Test phone', source_id='member1')) == 1
        assert [row['source_id'] for row in listed(client, category='unclassified')] == ['member2']
        assert client.get(DETAIL, auth=VIEWER).json()['clips'][0]['categories'] == ['obstacle', 'bus']
        assert save(client, [], 1).status_code == 200
        assert len(listed(client, category='unclassified')) == 2


def test_classification_survives_upload_restart_delete_restore(client, settings):
    files = seed_session(client)
    assert save(client, ['traffic_light', 'bus']).status_code == 200
    for path, data in files.items():
        assert upload(client, path, data).status_code == 200
    assert listed(client)[0]['categories'] == ['traffic_light', 'bus']
    deleted = remove_session(client).json()['item']
    assert save(client, [], 1).status_code == 404
    assert listed(client, category='bus') == []
    with TestClient(create_app(settings)) as restarted:
        assert restarted.post(f"/api/admin/trash/{deleted['id']}/restore", auth=VIEWER,
                              headers=ADMIN_HEADERS).status_code == 200
        assert listed(restarted)[0]['categories'] == ['traffic_light', 'bus']
        assert restarted.get(DETAIL, auth=VIEWER).json()['clips'][0]['category_revision'] == 1
        for path, data in files.items():
            expected = data if isinstance(data, bytes) else None
            if expected is not None:
                assert restarted.get(f'/api/files/member1/{path}', auth=VIEWER).content == expected


def test_auth_csrf_validation_and_unknown_session(client):
    seed_session(client)
    payload = {'categories': ['bus'], 'revision': 0}
    assert client.put(URL, json=payload, headers=HEADERS).status_code == 401
    assert client.put(URL, json=payload, headers={**HEADERS, 'Authorization': f"Bearer {TOKENS['member1']}"}).status_code == 401
    assert client.put(URL, json=payload, auth=VIEWER).status_code == 403
    assert client.put(URL, json=payload, auth=VIEWER, headers={**HEADERS, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    for invalid in (['unknown'], ['bus']*4, [1], 'bus', None):
        assert save(client, invalid).status_code == 422
    for revision in (-1, True, '0'):
        assert save(client, ['bus'], revision).status_code == 422
    assert client.put(URL.replace(SID, 'b'*32), json=payload, auth=VIEWER, headers=HEADERS).status_code == 404
    assert client.get('/api/sessions?category=unknown', auth=VIEWER).status_code == 400
    assert client.delete(f'/api/admin/sessions/member1/{SID}', auth=VIEWER).status_code == 403
    assert listed(client)[0]['categories'] == []


def test_concurrent_teammates_do_not_silently_overwrite(client):
    seed_session(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda values: save(client, values), [['bus'], ['obstacle']]))
    assert sorted(result.status_code for result in results) == [200, 409]
    stored = client.get(DETAIL, auth=VIEWER).json()['clips'][0]
    assert stored['category_revision'] == 1
    assert stored['categories'] in (['bus'], ['obstacle'])
    assert save(client, ['bus', 'obstacle'], 1).status_code == 200


def seed_clip(client, number, source="member1"):
    return upload(client, f"sessions/{SID}/clips/clip_{number:03d}/manifest.json",
                  {"clip_id": number, "state": "ready"}, source)


def save_clip(client, key, values, revision=0, source="member1"):
    return client.put(f"/api/sessions/{source}/{SID}/clips/{key}/categories", auth=VIEWER,
                      headers=HEADERS, json={"categories": values, "revision": revision})


def test_mixed_clips_are_independent_and_unclassified_filter_is_per_clip(client):
    seed_session(client)
    seed_session(client, 'member2')
    assert seed_clip(client, 2).status_code == 200
    assert seed_clip(client, 3).status_code == 200
    assert seed_clip(client, 4).status_code == 200
    assert save_clip(client, 'clip_001', ['bus']).status_code == 200
    assert save_clip(client, 'clip_002', ['traffic_light']).status_code == 200
    assert save_clip(client, 'clip_003', ['obstacle', 'bus']).status_code == 200
    detail = client.get(DETAIL, auth=VIEWER).json()
    assert [clip['categories'] for clip in detail['clips']] == [['bus'], ['traffic_light'], ['obstacle', 'bus'], []]
    assert [clip['category_revision'] for clip in detail['clips']] == [1, 1, 1, 0]
    assert 'category_revision' not in detail
    row = listed(client, category='bus')[0]
    assert row['matched_clip_count'] == 2 and row['clip_count'] == 4
    assert row['categories'] == ['obstacle', 'traffic_light', 'bus']
    assert row['unclassified_clip_count'] == 1
    assert listed(client, category='traffic_light')[0]['matched_clip_count'] == 1
    assert listed(client, category='obstacle')[0]['matched_clip_count'] == 1
    assert len(listed(client, category='unclassified')) == 2
    other = client.get(DETAIL.replace('member1', 'member2'), auth=VIEWER).json()
    assert other['clips'][0]['categories'] == []
    # Clearing one clip never changes siblings or another server's matching clip ID.
    assert save_clip(client, 'clip_001', [], 1).status_code == 200
    assert listed(client, category='bus')[0]['matched_clip_count'] == 1
    assert seed_clip(client, 5).status_code == 200
    assert listed(client, source_id='member1', category='unclassified')[0]['matched_clip_count'] == 3
    assert save_clip(client, 'clip_999', ['bus']).status_code == 404
    assert save_clip(client, 'bad-key', ['bus']).status_code == 400
    assert client.put(DETAIL + '/categories', json={'categories': ['bus'], 'revision': 0},
                      auth=VIEWER, headers=HEADERS).status_code in (404, 405)


def test_legacy_pair_is_independent_and_old_session_labels_are_not_propagated(client, settings):
    seed_session(client)
    assert save_clip(client, 'legacy', ['bus']).status_code == 404
    assert upload(client, f'sessions/{SID}/camera.mp4', b'legacy recording').status_code == 200
    with client.app.state.archive.connection() as db:
        db.execute('CREATE TABLE session_categories (source_id TEXT, session_id TEXT, categories TEXT, revision INTEGER)')
        db.execute('INSERT INTO session_categories VALUES (?, ?, ?, ?)', ('member1', SID, '["bus"]', 1))
    with TestClient(create_app(settings)) as restarted:
        clips = restarted.get(DETAIL, auth=VIEWER).json()['clips']
        assert len(clips) == 2
        assert all(clip['categories'] == [] for clip in clips)
        assert clips[1]['category_key'] == 'legacy'
        assert save_clip(restarted, 'legacy', ['obstacle']).status_code == 200
        assert save_clip(restarted, 'clip_001', ['bus']).status_code == 200
        assert listed(restarted, category='bus')[0]['matched_clip_count'] == 1
        assert listed(restarted, category='obstacle')[0]['matched_clip_count'] == 1
        with restarted.app.state.archive.connection() as db:
            assert db.execute('SELECT categories FROM session_categories').fetchone()[0] == '["bus"]'


def test_sessions_without_clips_do_not_match_unclassified(client):
    assert upload(client, f'sessions/{SID}/session.json', {'session_id': SID}).status_code == 200
    assert len(listed(client)) == 1
    assert listed(client, category='unclassified') == []
    assert listed(client, category='bus') == []
