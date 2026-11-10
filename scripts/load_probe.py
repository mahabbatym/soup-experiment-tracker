"""Short 500 events/s pulse with 50 simultaneous SSE subscribers.

Development-only probe, NOT a substitute for soak or production load testing.
Run: python scripts/load_probe.py
"""
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
N_RUNS = 50
EVENTS_PER_SECOND_PER_RUN = 10
ROUNDS = 4


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


async def run_probe(base, database):
    limits=httpx.Limits(max_connections=220,max_keepalive_connections=120)
    async with httpx.AsyncClient(limits=limits,timeout=httpx.Timeout(12,read=None)) as client:
        project=(await client.post(base+'/api/projects',json={'name':'Pulse test'})).json()
        headers={'X-API-Key':project['api_key']}
        runs=[]
        for i in range(N_RUNS):
            resp=await client.post(base+'/api/runs',headers=headers,json={'name':f'load-{i:02d}'})
            resp.raise_for_status()
            runs.append(resp.json()['id'])

        async def subscriber(run,ready):
            numbers=[]
            async with client.stream('GET',base+f'/api/runs/{run}/stream',headers=headers) as response:
                response.raise_for_status()
                ready.set()
                cursor=None
                async for line in response.aiter_lines():
                    if line.startswith('id:'):
                        cursor=int(line[3:].strip())
                    elif line.startswith('data:'):
                        obj=json.loads(line[5:])
                        assert cursor == obj['seq']
                        numbers.append(cursor)
                        if len(numbers)>=ROUNDS*EVENTS_PER_SECOND_PER_RUN:
                            return numbers
            return numbers

        readies=[asyncio.Event() for _ in runs]
        subs=[asyncio.create_task(subscriber(r,ready)) for r,ready in zip(runs,readies)]
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in readies)),timeout=15)
        print('streams_opened:',len(subs),flush=True)
        latencies=[]
        failures=[]
        t0=time.perf_counter()
        for round_num in range(ROUNDS):
            await asyncio.sleep(max(0,(t0+round_num)-time.perf_counter()))
            observed=datetime.now(timezone.utc).isoformat()
            async def post_batch(j,run):
                events=[{'event_id':str(uuid.uuid4()),'name':'accuracy','value':round_num*10+k,
                         'observed_at':observed} for k in range(EVENTS_PER_SECOND_PER_RUN)]
                start=time.perf_counter()
                try:
                    result=await client.post(base+f'/api/runs/{run}/metrics',headers=headers,
                          json={'batch_id':str(uuid.uuid4()),'events':events},timeout=10)
                    if result.status_code!=200 or result.json()['accepted']!=10:
                        failures.append(f'{j}: HTTP {result.status_code} {result.text[:200]}')
                except Exception as exc:
                    failures.append(f'{j}: {exc!r}')
                finally:
                    latencies.append((time.perf_counter()-start)*1000)
            await asyncio.gather(*(post_batch(j,r) for j,r in enumerate(runs)))
            print(f'round_{round_num+1}_complete: +{N_RUNS*EVENTS_PER_SECOND_PER_RUN} events',flush=True)
        ingest_time=time.perf_counter()-t0
        try:
            received=await asyncio.wait_for(asyncio.gather(*subs),timeout=20)
        except Exception:
            for task in subs:task.cancel()
            raise
        with sqlite3.connect(database) as con:
            committed=con.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]
            run_counts=con.execute('SELECT event_count FROM runs').fetchall()
        expected=N_RUNS*ROUNDS*EVENTS_PER_SECOND_PER_RUN
        all_gapfree=all(nums==list(range(1,ROUNDS*EVENTS_PER_SECOND_PER_RUN+1)) for nums in received)
        all_counts=all(count[0]==ROUNDS*EVENTS_PER_SECOND_PER_RUN for count in run_counts)
        observed_rate=expected/ingest_time
        print('expected_events:',expected)
        print('db_committed:',committed)
        print('stream_received:',sum(map(len,received)))
        print('all_cursors_gap_free:',all_gapfree)
        print('all_db_run_counts_match:',all_counts)
        print('http_failures:',failures)
        print('ingest_window_seconds:',round(ingest_time,3))
        print('observed_overall_ingest_rate_ev_per_s:',round(observed_rate,1))
        print('http_batch_latency_ms_max:',round(max(latencies),1))
        print('http_batch_latency_ms_avg:',round(sum(latencies)/len(latencies),1))
        assert committed == expected and sum(map(len,received))==expected
        assert all_gapfree and all_counts and not failures
        print('RESULT: PASS (short local pulse; not production validation)')


async def main():
    with tempfile.TemporaryDirectory(prefix='soup-load-') as root:
        db=Path(root)/'run.sqlite3';port=free_port();base=f'http://127.0.0.1:{port}'
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1',
                               '--port',str(port),'--no-access-log'],cwd=ROOT,
                              env={**os.environ,'DATABASE_PATH':str(db)},
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        try:
            async with httpx.AsyncClient(timeout=1) as c:
                for _ in range(100):
                    try:
                        if (await c.get(base+'/api/health')).status_code==200:break
                    except httpx.HTTPError:pass
                    await asyncio.sleep(.1)
                else:raise RuntimeError('Server startup failed')
            await run_probe(base,db)
        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill()


if __name__=='__main__':
    asyncio.run(main())
