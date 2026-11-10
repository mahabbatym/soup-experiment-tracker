"""Tenant-scoped SQLite persistence. Each operation owns a short-lived connection.

All writers use BEGIN IMMEDIATE, so per-run server sequences are allocated in
commit order. All statements use bound parameters, never string interpolation.
"""
import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def issue_key():
    return 'soup_' + secrets.token_urlsafe(32)


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=30000')
        try:
            yield con
        finally:
            con.close()

    def init(self):
        with self.connection() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=FULL')
            con.executescript('''
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                key_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL, next_seq INTEGER NOT NULL DEFAULT 1,
                event_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_by_project ON runs(project_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS metrics (
                run_id TEXT NOT NULL REFERENCES runs(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                seq INTEGER NOT NULL, event_id TEXT NOT NULL,
                name TEXT NOT NULL, value REAL NOT NULL,
                observed_at TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY(run_id,seq), UNIQUE(run_id,event_id)
            );
            CREATE INDEX IF NOT EXISTS metrics_tenant_cursor
                ON metrics(project_id,run_id,seq);
            CREATE TABLE IF NOT EXISTS metric_batches (
                run_id TEXT NOT NULL REFERENCES runs(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                batch_id TEXT NOT NULL, digest TEXT NOT NULL,
                response TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(run_id,batch_id)
            );
            ''')

    def create_project(self, name):
        project_id, key = str(uuid.uuid4()), issue_key()
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT INTO projects VALUES (?,?,?)', (project_id, name, utcnow()))
            con.execute('INSERT INTO api_keys VALUES (?,?,?,?,NULL)',
                        (str(uuid.uuid4()), project_id, token_hash(key), utcnow()))
            con.commit()
        return {'project_id': project_id, 'name': name, 'api_key': key}

    def authenticate(self, key):
        if not key or len(key) > 256:
            return None
        with self.connection() as con:
            row = con.execute('''SELECT p.id, p.name, p.created_at
                FROM api_keys k JOIN projects p ON p.id=k.project_id
                WHERE k.key_hash=? AND k.revoked_at IS NULL''', (token_hash(key),)).fetchone()
            return dict(row) if row else None

    def rotate_key(self, project_id, old_key):
        new_key = issue_key()
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            result = con.execute('''UPDATE api_keys SET revoked_at=?
                WHERE project_id=? AND key_hash=? AND revoked_at IS NULL''',
                (utcnow(), project_id, token_hash(old_key)))
            if result.rowcount != 1:
                con.rollback()
                raise Missing()
            con.execute('INSERT INTO api_keys VALUES (?,?,?,?,NULL)',
                        (str(uuid.uuid4()), project_id, token_hash(new_key), utcnow()))
            con.commit()
        return {'api_key': new_key}

    def create_run(self, project_id, name):
        run_id, created = str(uuid.uuid4()), utcnow()
        with self.connection() as con:
            con.execute('INSERT INTO runs (id,project_id,name,created_at) VALUES (?,?,?,?)',
                        (run_id, project_id, name, created))
        return {'id': run_id, 'name': name, 'created_at': created, 'event_count': 0, 'high_water': 0}

    def get_run(self, project_id, run_id):
        with self.connection() as con:
            row = con.execute('''SELECT id,name,created_at,event_count,next_seq-1 AS high_water
                FROM runs WHERE id=? AND project_id=?''', (run_id, project_id)).fetchone()
            if row is None:
                raise Missing()
            return dict(row)

    def list_runs(self, project_id):
        with self.connection() as con:
            rows = con.execute('''SELECT id,name,created_at,event_count,next_seq-1 AS high_water
                FROM runs WHERE project_id=? ORDER BY created_at DESC,id DESC''', (project_id,)).fetchall()
            return [dict(row) for row in rows]

    def add_batch(self, project_id, run_id, batch_id, events):
        canonical = [
            {'event_id': str(e.event_id), 'name': e.name, 'value': e.value,
             'observed_at': e.observed_at.isoformat()}
            for e in events
        ]
        payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            run = con.execute('''SELECT next_seq FROM runs WHERE id=? AND project_id=?''',
                              (run_id, project_id)).fetchone()
            if not run:
                con.rollback()
                raise Missing()
            existing_batch = con.execute('''SELECT digest,response FROM metric_batches
                WHERE run_id=? AND project_id=? AND batch_id=?''',
                (run_id, project_id, str(batch_id))).fetchone()
            if existing_batch:
                if existing_batch['digest'] != digest:
                    con.rollback()
                    raise Conflict('batch_id reused with different content')
                response = json.loads(existing_batch['response'])
                con.commit()
                return response

            seq = run['next_seq']
            inserted = 0
            acknowledgements = []
            for event in canonical:
                previous = con.execute('''SELECT seq,name,value,observed_at FROM metrics
                    WHERE run_id=? AND project_id=? AND event_id=?''',
                    (run_id, project_id, event['event_id'])).fetchone()
                if previous:
                    if (previous['name'] != event['name'] or
                        previous['value'] != event['value'] or
                        previous['observed_at'] != event['observed_at']):
                        con.rollback()
                        raise Conflict('event_id reused with different content')
                    acknowledgements.append({'event_id': event['event_id'], 'seq': previous['seq'], 'duplicate': True})
                    continue
                con.execute('''INSERT INTO metrics
                    (run_id,project_id,seq,event_id,name,value,observed_at,received_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (run_id, project_id, seq, event['event_id'], event['name'], event['value'],
                     event['observed_at'], utcnow()))
                acknowledgements.append({'event_id': event['event_id'], 'seq': seq, 'duplicate': False})
                seq += 1
                inserted += 1
            con.execute('UPDATE runs SET next_seq=?, event_count=event_count+? WHERE id=? AND project_id=?',
                        (seq, inserted, run_id, project_id))
            response = {'batch_id': str(batch_id), 'accepted': inserted,
                        'duplicates': len(events)-inserted, 'events': acknowledgements,
                        'high_water': seq-1}
            con.execute('''INSERT INTO metric_batches VALUES (?,?,?,?,?,?)''',
                        (run_id, project_id, str(batch_id), digest,
                         json.dumps(response, separators=(',', ':')), utcnow()))
            con.commit()
            return response

    def events_after(self, project_id, run_id, after=0, limit=200):
        # Crucial: tenant predicate is present in *every* data access path.
        with self.connection() as con:
            run = con.execute('''SELECT next_seq-1 AS high_water FROM runs
                WHERE id=? AND project_id=?''', (run_id, project_id)).fetchone()
            if not run:
                raise Missing()
            rows = con.execute('''SELECT seq,event_id,name,value,observed_at,received_at
                FROM metrics WHERE project_id=? AND run_id=? AND seq>?
                ORDER BY seq ASC LIMIT ?''', (project_id, run_id, after, limit)).fetchall()
            items = [dict(row) for row in rows]
            last_seq = items[-1]['seq'] if items else after
            return {'events': items, 'high_water': run['high_water'],
                    'has_more': last_seq < run['high_water']}
