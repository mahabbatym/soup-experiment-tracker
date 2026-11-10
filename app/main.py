"""FastAPI application and durable-cursor SSE stream."""
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import Conflict, Missing, Store

ROOT = Path(__file__).resolve().parent


class Named(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator('name')
    @classmethod
    def trim(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Name is required')
        return value


class Metric(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: UUID
    name: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z][A-Za-z0-9_.-]*$')
    value: float = Field(allow_inf_nan=False)
    observed_at: datetime

    @field_validator('observed_at')
    @classmethod
    def needs_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Timezone required (ISO 8601, e.g. Z)')
        return value


class Batch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batch_id: UUID
    events: list[Metric] = Field(min_length=1, max_length=250)

    @field_validator('events')
    @classmethod
    def unique_ids(cls, events):
        if len(set(str(x.event_id) for x in events)) != len(events):
            raise ValueError('Duplicate event_id in one batch')
        return events


def create_app(db_path=None):
    app = FastAPI(title='Soup Experiment Tracker', version='1.0.0', docs_url='/api/docs',
                  redoc_url=None, openapi_url='/api/openapi.json')
    app.state.store = Store(db_path or os.environ.get('DATABASE_PATH', './data/tracker.sqlite3'))

    @app.middleware('http')
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(Missing)
    async def missing_handler(request, exc):
        return JSONResponse({'detail': 'Not found'}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    def project(request: Request, x_api_key: Annotated[str | None, Header()] = None):
        current = request.app.state.store.authenticate(x_api_key)
        if current is None:
            raise HTTPException(status_code=401, detail='Invalid API key')
        return current

    @app.get('/api/health')
    def health():
        return {'status': 'ok'}

    @app.post('/api/projects', status_code=201)
    def create_project(data: Named, request: Request):
        return request.app.state.store.create_project(data.name)

    @app.get('/api/me')
    def me(current=Depends(project)):
        return current

    @app.post('/api/keys/rotate')
    def rotate(request: Request, current=Depends(project),
               x_api_key: Annotated[str | None, Header()] = None):
        return request.app.state.store.rotate_key(current['id'], x_api_key)

    @app.get('/api/runs')
    def list_runs(request: Request, current=Depends(project)):
        return request.app.state.store.list_runs(current['id'])

    @app.post('/api/runs', status_code=201)
    def new_run(data: Named, request: Request, current=Depends(project)):
        return request.app.state.store.create_run(current['id'], data.name)

    @app.get('/api/runs/{run_id}')
    def get_run(run_id: UUID, request: Request, current=Depends(project)):
        return request.app.state.store.get_run(current['id'], str(run_id))

    @app.post('/runs/{run_id}/metrics')
    @app.post('/api/runs/{run_id}/metrics')
    def ingest(run_id: UUID, data: Batch, request: Request, current=Depends(project)):
        return request.app.state.store.add_batch(current['id'], str(run_id), data.batch_id, data.events)

    @app.get('/api/runs/{run_id}/metrics')
    def get_metrics(run_id: UUID, request: Request, current=Depends(project),
                    after: int = Query(default=0, ge=0),
                    limit: int = Query(default=200, ge=1, le=500)):
        return request.app.state.store.events_after(current['id'], str(run_id), after, limit)

    @app.get('/api/runs/{run_id}/stream')
    async def stream(run_id: UUID, request: Request, current=Depends(project),
                     last_event_id: Annotated[str | None, Header()] = None,
                     after: int = Query(default=0, ge=0)):
        # Authorize before opening the stream: denied streams must return 404,
        # not a 200 response that later reveals an event/error.
        run_id_text = str(run_id)
        run = request.app.state.store.get_run(current['id'], run_id_text)
        if last_event_id is not None:
            if not last_event_id.isascii() or not last_event_id.isdecimal():
                raise HTTPException(status_code=400, detail='Invalid Last-Event-ID')
            after = int(last_event_id)
        if after > 2**63-1 or after > run['high_water']:
            raise HTTPException(status_code=400, detail='Cursor is too large')
        tenant_id = current['id']
        store = request.app.state.store

        async def iterator():
            cursor = after
            last_ping = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                # Durable DB reads instead of per-client memory queues. The
                # StreamingResponse sender applies backpressure naturally.
                page = await asyncio.to_thread(store.events_after, tenant_id, run_id_text, cursor, 100)
                if page['events']:
                    for event in page['events']:
                        cursor = event['seq']
                        yield f"id: {cursor}\nevent: metric\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                    last_ping = asyncio.get_running_loop().time()
                    continue
                now = asyncio.get_running_loop().time()
                if now - last_ping >= 10:
                    yield ': heartbeat\n\n'
                    last_ping = now
                await asyncio.sleep(0.25)

        return StreamingResponse(iterator(), media_type='text/event-stream', headers={
            'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no',
        })

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


app = create_app()
