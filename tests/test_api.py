"""API, DB transaction, replay, tenant isolation and silent-failure probes."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from conftest import batch, event, hdr, make_project, make_run, post_metrics


def test_project_creation_and_auth(client):
    a = make_project(client)
    assert a['api_key'].startswith('soup_')
    assert client.get('/api/me').status_code == 401
    assert client.get('/api/me', headers={'X-API-Key': 'invalid'}).status_code == 401
    me = client.get('/api/me', headers=hdr(a)).json()
    assert me['id'] == a['project_id']
    assert 'api_key' not in me
    assert client.get('/api/me', headers=hdr(a)).headers['cache-control'] == 'no-store'


def test_batches_are_idempotent_and_ordered_by_commit_not_client_time(client):
    a, r = make_project(client), None
    r = make_run(client, a)
    old, fresh = event(.2, age_s=60), event(.8, age_s=0)
    payload = batch(fresh, old)  # observed_at is out of order
    first = post_metrics(client, a, r, payload)
    assert first.status_code == 200, first.text
    assert first.json()['accepted'] == 2
    retry = post_metrics(client, a, r, payload)
    assert retry.status_code == 200
    assert retry.json() == first.json()
    new_batch = post_metrics(client, a, r, batch(old, event(.6, age_s=3600)))
    assert new_batch.status_code == 200, new_batch.text
    assert new_batch.json()['accepted'] == 1
    assert new_batch.json()['duplicates'] == 1
    snapshot = client.get(f"/api/runs/{r['id']}/metrics", headers=hdr(a)).json()
    assert [x['seq'] for x in snapshot['events']] == [1, 2, 3]
    assert [x['value'] for x in snapshot['events']] == [.8, .2, .6]
    assert client.get(f"/api/runs/{r['id']}", headers=hdr(a)).json()['event_count'] == 3


def test_batch_id_mutation_rejected_and_no_partial_write(client):
    p = make_project(client)
    r = make_run(client, p)
    e = event(7)
    first = batch(e)
    assert post_metrics(client, p, r, first).status_code == 200
    altered = {'batch_id': first['batch_id'], 'events': [event(3)]}
    assert post_metrics(client, p, r, altered).status_code == 409
    # Event conflict occurs *after* a new event in the same batch:
    # full transaction must roll back, including the new event.
    conflicting = batch(event(20), dict(e, value=999))
    assert post_metrics(client, p, r, conflicting).status_code == 409
    snap = client.get(f"/api/runs/{r['id']}/metrics", headers=hdr(p)).json()
    assert [v['value'] for v in snap['events']] == [7]
    assert snap['high_water'] == 1


def test_tenant_cannot_read_write_or_subscribe_and_unknown_ids_are_uniform(client):
    a, b = make_project(client, 'Tenant A'), make_project(client, 'Tenant B')
    ra, rb = make_run(client, a, 'Private A'), make_run(client, b, 'Private B')
    assert post_metrics(client, b, rb, batch(event(42))).status_code == 200
    # Positive control: B sees only its own event, and A cannot see B's run.
    assert len(client.get('/api/runs', headers=hdr(b)).json()) == 1
    assert rb['id'] not in str(client.get('/api/runs', headers=hdr(a)).json())
    target = f"/api/runs/{rb['id']}"
    assert client.get(target, headers=hdr(a)).status_code == 404
    assert client.get(target + '/metrics', headers=hdr(a)).status_code == 404
    assert post_metrics(client, a, rb, batch(event(44))).status_code == 404
    assert client.get(target + '/stream', headers=hdr(a)).status_code == 404
    # ID guessing, including previously cached read, returns the *same* 404.
    other_id = uuid4()
    for path in [target, f'/api/runs/{other_id}', target, target + '/metrics']:
        response = client.get(path, headers=hdr(a))
        assert response.status_code == 404
        assert response.json() == {'detail': 'Not found'}
        assert '42' not in response.text and 'Private B' not in response.text
    assert client.get(target, headers=hdr(b)).status_code == 200
    assert client.get(target, headers=hdr(a)).status_code == 404
    assert client.get(f"/api/runs/{ra['id']}", headers=hdr(a)).status_code == 200


def test_key_rotation_old_key_stops_everywhere_immediately(client):
    p = make_project(client)
    r = make_run(client, p)
    result = client.post('/api/keys/rotate', headers=hdr(p))
    assert result.status_code == 200
    new_key = result.json()['api_key']
    assert new_key != p['api_key']
    paths = [f"/api/runs/{r['id']}", f"/api/runs/{r['id']}/metrics",
             f"/api/runs/{r['id']}/stream", '/api/runs', '/api/me']
    for path in paths:
        assert client.get(path, headers=hdr(p)).status_code == 401
        if 'stream' not in path:
            assert client.get(path, headers={'X-API-Key': new_key}).status_code == 200
    assert post_metrics(client, p, r, batch(event(1))).status_code == 401
    assert client.post('/api/keys/rotate', headers=hdr(p)).status_code == 401
    assert client.post(f"/api/runs/{r['id']}/metrics", headers={'X-API-Key': new_key},
                       json=batch(event(2))).status_code == 200


def test_reconnect_cursor_catches_up_all_missed_events(client):
    p = make_project(client)
    r = make_run(client, p)
    first = batch(event(1), event(2))
    assert post_metrics(client, p, r, first).status_code == 200
    cursor = 2
    # Simulate no client subscription; later events are persisted durably.
    assert post_metrics(client, p, r, batch(event(3), event(4))).status_code == 200
    assert post_metrics(client, p, r, first).status_code == 200
    missed = client.get(f"/api/runs/{r['id']}/metrics?after={cursor}", headers=hdr(p)).json()
    assert [v['seq'] for v in missed['events']] == [3, 4]
    assert [v['value'] for v in missed['events']] == [3, 4]
    assert client.get(f"/api/runs/{r['id']}/stream", headers={**hdr(p), 'Last-Event-ID':'999'}).status_code == 400


def test_slow_reader_uses_bounded_pages_with_no_queue_or_loss(client):
    p = make_project(client)
    r = make_run(client, p)
    all_events = [event(float(i)) for i in range(570)]
    for i in range(0,570,190):
        assert post_metrics(client,p,r,batch(*all_events[i:i+190])).status_code == 200
    cursor,seen = 0,[]
    while True:
        page = client.get(f"/api/runs/{r['id']}/metrics?after={cursor}&limit=17",headers=hdr(p)).json()
        seen.extend(v['seq'] for v in page['events'])
        if page['events']:cursor=page['events'][-1]['seq']
        if not page['has_more']:break
    assert seen == list(range(1,571))
    assert client.get(f"/api/runs/{r['id']}",headers=hdr(p)).json()['event_count'] == 570


def test_invalid_payloads_are_rejected_without_side_effects(client):
    p = make_project(client)
    r = make_run(client,p)
    e = event(3)
    cases = [
        batch(e,e),
        batch(dict(e, observed_at='2026-01-01T00:00:00')),
        batch(dict(e,name='bad name')),
        batch(dict(e,value='NaN')),
        batch(*[event(i) for i in range(251)]),
    ]
    for payload in cases:
        assert post_metrics(client,p,r,payload).status_code == 422
    assert client.get(f"/api/runs/{r['id']}/metrics",headers=hdr(p)).json()['events'] == []


def test_parallel_writers_allocate_gap_free_sequences(client):
    p = make_project(client)
    r = make_run(client,p)
    payloads = [batch(event(i)) for i in range(36)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda b: post_metrics(client,p,r,b).status_code,payloads))
    assert all(code==200 for code in results), results
    snapshot=client.get(f"/api/runs/{r['id']}/metrics",headers=hdr(p)).json()
    assert [e['seq'] for e in snapshot['events']] == list(range(1,37))


def test_key_not_returned_in_lists_or_run_detail(client):
    p = make_project(client)
    r = make_run(client,p)
    for path in ['/api/runs',f"/api/runs/{r['id']}",'/api/me']:
        response = client.get(path,headers=hdr(p))
        assert p['api_key'] not in response.text
        assert response.headers['referrer-policy'] == 'no-referrer'


def test_exact_assignment_ingest_route_alias(client):
    p=make_project(client)
    r=make_run(client,p)
    result=client.post(f"/runs/{r['id']}/metrics",headers=hdr(p),json=batch(event(.33)))
    assert result.status_code==200
    assert result.json()['high_water']==1
    assert client.get(f"/api/runs/{r['id']}/metrics",headers=hdr(p)).json()['events'][0]['value']==.33
