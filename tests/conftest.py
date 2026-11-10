import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'test.db')) as c:
        yield c


def make_project(client, name='Alpha'):
    response = client.post('/api/projects', json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()


def hdr(project):
    return {'X-API-Key': project['api_key']}


def make_run(client, project, name='baseline'):
    response = client.post('/api/runs', headers=hdr(project), json={'name': name})
    assert response.status_code == 201, response.text
    return response.json()


def event(value, name='accuracy', age_s=0, event_id=None):
    return {
        'event_id': event_id or str(uuid.uuid4()), 'name': name, 'value': value,
        'observed_at': (datetime.now(timezone.utc)-timedelta(seconds=age_s)).isoformat(),
    }


def batch(*events, batch_id=None):
    return {'batch_id': batch_id or str(uuid.uuid4()), 'events': list(events)}


def post_metrics(client, project, run, payload):
    return client.post(f"/api/runs/{run['id']}/metrics", headers=hdr(project), json=payload)
