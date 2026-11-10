"""Real browser + HTTP API + actual persisted SQLite database.

Requires Chromium and Playwright. Execute: pytest tests/test_e2e.py -v
"""
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright, Error as PlaywrightError

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    path = tmp_path_factory.mktemp('live-ui') / 'tracker.sqlite3'
    port = free_port()
    env = {**os.environ, 'DATABASE_PATH': str(path)}
    process = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
         '--port', str(port), '--no-access-log'], cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('Uvicorn terminated: ' + process.stderr.read())
            try:
                if httpx.get(base + '/api/health', timeout=0.4).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError('Uvicorn failed to start')
        yield base, path
    finally:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()


def send_batch(base, key, run, events, batch_id=None):
    payload = {'batch_id': batch_id or str(uuid.uuid4()), 'events': events}
    r = httpx.post(base+f'/api/runs/{run}/metrics', headers={'X-API-Key': key},
                   json=payload, timeout=6)
    assert r.status_code == 200, r.text
    return payload, r.json()


def metric(name, value, observed_age):
    return {'event_id': str(uuid.uuid4()), 'name': name, 'value': value,
            'observed_at': (datetime.now(timezone.utc)-timedelta(seconds=observed_age)).isoformat()}


def test_browser_api_db_match_after_reorder_duplicate_late_disconnect(server):
    base, database_path = server
    with sync_playwright() as p:
        browser_path = os.environ.get('SOUP_CHROMIUM_PATH') or shutil.which('chromium')
        options = {'executable_path': browser_path} if browser_path else {}
        browser = p.chromium.launch(headless=True,
                                    args=['--no-sandbox','--disable-dev-shm-usage'], **options)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            try:
                page.goto(base, wait_until='networkidle')
            except PlaywrightError as exc:
                if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
                    pytest.skip('Chromium managed URLBlocklist denies localhost; run on an unrestricted browser')
                raise
            assert page.locator('#welcome').is_visible()
            page.locator('#projectName').fill('Browser QA')
            page.locator('#createProjectForm button[type=submit]').click()
            key_field = page.locator('#newKey')
            key_field.wait_for(state='visible')
            key = key_field.input_value()
            assert key.startswith('soup_')
            page.locator('#enterProject').click()
            page.locator('#dashboard').wait_for(state='visible')
            page.locator('#newRunButton').click()
            page.locator('#runName').fill('Reconnect and consistency proof')
            page.locator('#newRunForm button[type=submit]').click()
            page.locator('#runView').wait_for(state='visible')
            run = page.locator('#runIdText').inner_text()
            assert str(uuid.UUID(run)) == run
            page.wait_for_function("document.getElementById('liveText').textContent.includes('connected')")

            # Deliberately commit client timestamps out of order.
            first = [metric('accuracy', .82, 0), metric('accuracy', .35, 50),
                     metric('loss', .7, 25)]
            batch_one, result = send_batch(base,key,run,first)
            assert result['accepted'] == 3
            page.wait_for_function("document.getElementById('detailCount').textContent === '3'")

            # Inject a real fetch/stream abort; while disconnected commit more
            # metrics, including a late measurement and a duplicate retry.
            page.evaluate("window.__trackerDebug.disconnect(1800)")
            assert page.wait_for_function("document.getElementById('liveText').textContent.includes('Reconnecting')")
            second = [metric('accuracy', .91, 2), metric('accuracy', .25, 3600)]
            batch_two, result = send_batch(base,key,run,second)
            assert result['accepted'] == 2
            duplicate = httpx.post(base+f'/api/runs/{run}/metrics',
                                   headers={'X-API-Key': key}, json=batch_one, timeout=5)
            assert duplicate.status_code == 200
            assert duplicate.json()['accepted'] == 3  # original stored acknowledgement
            duplicate_2 = httpx.post(base+f'/api/runs/{run}/metrics',
                                     headers={'X-API-Key': key}, json=batch_two, timeout=5)
            assert duplicate_2.status_code == 200

            page.wait_for_function("document.getElementById('detailCount').textContent === '5'", timeout=12000)
            page.wait_for_function("document.getElementById('liveText').textContent.includes('connected')", timeout=12000)
            assert page.locator('#detailCursor').inner_text() == '5'
            assert page.locator('#detailMetrics').inner_text() == '2'
            assert page.locator('#eventsBody tr').count() == 5
            assert page.locator('#currentValue').inner_text() == '0.25'  # late observation is last commit

            # Independent database query: compare all visible sequence IDs,
            # names, values to actual durable committed records.
            with sqlite3.connect(database_path) as con:
                records = con.execute('SELECT seq,name,value FROM metrics WHERE run_id=? ORDER BY seq DESC',
                                      (run,)).fetchall()
                db_count = con.execute('SELECT event_count FROM runs WHERE id=?',(run,)).fetchone()[0]
            visible = page.locator('#eventsBody tr').evaluate_all('''rows => rows.map(r => {
                const t=[...r.querySelectorAll('td')].map(c=>c.textContent);
                return [parseInt(t[0].slice(1)),t[1],parseFloat(t[2])];
            })''')
            assert visible == [list(row) for row in records]
            assert db_count == len(records) == 5
            assert all(row[0]==6-index for index,row in enumerate(records,1))
            assert 'soup_' not in page.url  # secret is never a URL parameter

            # A second project's credentials may not read or subscribe to this run.
            other = httpx.post(base+'/api/projects',json={'name':'Other tenant'}).json()
            for url in [f'/api/runs/{run}', f'/api/runs/{run}/metrics', f'/api/runs/{run}/stream']:
                denied = httpx.get(base+url, headers={'X-API-Key':other['api_key']}, timeout=5)
                assert denied.status_code == 404
        finally:
            browser.close()
