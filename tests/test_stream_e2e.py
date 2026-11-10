"""Integration proof: real Uvicorn HTTP SSE disconnect/replay + DB equality."""
import json
import sqlite3
from uuid import uuid4

import httpx
from test_e2e import server, metric, send_batch  # shared live-server fixture


def receive_metric_seqs(base, key, run, last_id, expected):
    out = []
    with httpx.Client(timeout=httpx.Timeout(12, connect=3)) as client:
        with client.stream('GET', base+f'/api/runs/{run}/stream',
                           headers={'X-API-Key':key,'Last-Event-ID':str(last_id)}) as r:
            assert r.status_code == 200
            assert r.headers['content-type'].startswith('text/event-stream')
            current = {'id':None,'event':None,'data':None}
            for line in r.iter_lines():
                if line == '':
                    if current['event'] == 'metric':
                        body = json.loads(current['data'])
                        assert body['seq'] == int(current['id'])
                        out.append(body)
                        if len(out) >= expected:
                            break
                    current = {'id':None,'event':None,'data':None}
                elif line.startswith(('id:','event:','data:')):
                    key_part,value=line.split(':',1)
                    current[key_part]=value.strip()
    return out


def test_real_http_stream_disconnect_reconnect_and_database_match(server):
    base, db_path = server
    project = httpx.post(base+'/api/projects',json={'name':'Stream QA'}).json()
    key = project['api_key']
    r = httpx.post(base+'/api/runs',headers={'X-API-Key':key},json={'name':'wire test'}).json()
    run = r['id']
    first, acknowledged = send_batch(base,key,run,[metric('accuracy',.8,0),metric('loss',.4,3)])
    assert acknowledged['accepted'] == 2
    received_one = receive_metric_seqs(base,key,run,0,2)
    assert [e['seq'] for e in received_one] == [1,2]
    # Closing response context intentionally drops the TCP/SSE connection.
    second, _ = send_batch(base,key,run,[metric('accuracy',.9,2),metric('accuracy',.3,100)])
    retry = httpx.post(base+f'/api/runs/{run}/metrics',json=first,headers={'X-API-Key':key})
    assert retry.status_code == 200
    received_two = receive_metric_seqs(base,key,run,2,2)
    assert [e['seq'] for e in received_two] == [3,4]
    combined = received_one+received_two
    with sqlite3.connect(db_path) as db:
        actual = db.execute('SELECT seq,name,value FROM metrics WHERE run_id=? ORDER BY seq',(run,)).fetchall()
    assert [(e['seq'],e['name'],e['value']) for e in combined] == actual
    assert len(actual) == 4
    # Unknown tenant and valid-but-wrong tenant cannot open any stream.
    other = httpx.post(base+'/api/projects',json={'name':'Wrong project'}).json()
    assert httpx.get(base+f'/api/runs/{run}/stream',headers={'X-API-Key':other['api_key']}).status_code == 404
