# SOUP / Experiment Tracker

A take-home full-stack implementation of an **eventually consistent live experiment dashboard with durable replay and tenant isolation**. Stack: FastAPI + Python, SQLite WAL, native JavaScript and CSS, Server-Sent Events, Pytest, Playwright, Docker Compose.

**Key review artifacts:** `DESIGN_ONE_PAGE.pdf` (exactly 1 page), `SUBMISSION_NOTE.pdf` (exactly 2 pages), `TASK_CHECKLIST.md` (requirement-by-requirement status).

**Task coverage:** projects, project API keys, immediate key rotation, run list, batch metric ingest, retry idempotency, live run page, replay after disconnect, cross-tenant blocking, automated tests, one-page design, ≤2-page submission note and unedited test output.

## Run it — one command

```bash
docker compose up
```

Open **http://localhost:8000**. Create a workspace; **copy the API key** immediately (only shown once). Then open the workspace, create an experiment, and click **Add 12 demo events** or send events from the command line. The SQLite database persists inside the named Docker volume `tracker_data`; `docker compose down` preserves it, `docker compose down -v` **deletes it**. The Compose port is bound to `127.0.0.1` only, intentionally not publicly exposed.

The app should run on any host with Docker Engine + Compose v2. **Docker was not available in the authoring environment**; the Compose build is supplied but has not been locally verified there. `docker compose up` is the acceptance command, and the CI job performs an explicit Docker smoke test on a Docker-enabled runner.

### Local Python development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_PATH=./data/tracker.sqlite3  # Windows PowerShell: $env:DATABASE_PATH="./data/tracker.sqlite3"
uvicorn app.main:app --reload
```

App and docs: `http://127.0.0.1:8000/` and `/api/docs`. No external accounts or SaaS services required.

## API quick start

```bash
# Create a project. This returns the only copy of the API key.
curl -sS -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' -d '{"name":"My experiment project"}'

# Copy api_key from the response into your *shell*, not into your source code.
export SOUP_API_KEY='soup_REPLACE_WITH_YOUR_REAL_KEY'

# Create a run and copy its UUID from the response.
curl -sS -X POST http://localhost:8000/api/runs \
  -H "X-API-Key: $SOUP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"baseline"}'

export RUN_ID='REPLACE_WITH_RUN_UUID'

# Example (replace UUIDs with real generated UUIDs).
curl -sS -X POST "http://localhost:8000/runs/$RUN_ID/metrics" \
  -H "X-API-Key: $SOUP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"batch_id":"2f07c13e-d08e-4d50-b593-e5c24c53b0fa","events":[{"event_id":"4c25fa38-cfb6-4801-a5d4-36270b192916","name":"accuracy","value":0.94,"observed_at":"2026-11-10T09:00:00Z"}]}'
```

**API reference**

| Endpoint | Purpose |
|---|---|
| `POST /api/projects` | Creates a project and returns its **one-time** key (unauthenticated demo bootstrap) |
| `GET /api/me` | Identifies current project |
| `POST /api/keys/rotate` | Atomically revokes current key and returns replacement |
| `GET /api/runs`, `POST /api/runs` | Lists/creates runs scoped to API key |
| `GET /api/runs/{id}` | Run metadata, event count, high-water sequence |
| `POST /runs/{id}/metrics` | Batch ingestion; alias `/api/runs/{id}/metrics` |
| `GET /api/runs/{id}/metrics?after=0&limit=200` | Paginated committed history, after exclusive |
| `GET /api/runs/{id}/stream` | Tenant-authenticated SSE with `Last-Event-ID` header |
| `GET /api/health` | Container health |

All endpoints except project creation, health, UI static files and OpenAPI documentation require `X-API-Key` in the header. API secrets must not appear in URLs or logs. The browser uses `fetch()` to send headers for SSE: native `EventSource` does not support custom authentication headers. Preserve `batch_id` and `event_id` when retrying. If an identical batch is retransmitted, the original 200 acknowledgement is returned, *not* a new insert.

## How to run tests

```bash
python -m pytest -vv -ra                # API, real SSE + browser E2E
python scripts/load_probe.py           # 50 concurrent SSE streams, 500 events/sec target
```

For browser E2E, install Playwright's browser first: `python -m playwright install chromium`. On Linux install required libraries if needed: `python -m playwright install --with-deps chromium`. If system Chromium is installed it is chosen first; override with `SOUP_CHROMIUM_PATH=/path/to/chromium`. A browser policy that blocks `127.0.0.1` causes **only the browser test** to be marked skipped. API, real SSE and DB tests still run. CI has a fresh browser install and a Docker verification workflow.

### Evidence and failure transparency

`evidence/pytest_api.txt`, `evidence/pytest_final.txt`, `evidence/pytest_browser.txt` (the **original, unmodified failure**, not deleted), `evidence/load_probe.txt` and `evidence/docker_environment.txt` are original captured tool outputs. The browser test could not access localhost due to managed Chromium `URLBlocklist`; rerun in a browser not subject to that restriction before claiming full UI correctness. The browser test tests actual page state versus DB after out-of-order, duplicate, late and reconnect conditions; **it was not successful in this restricted environment**. The real HTTP + SSE + SQLite integration test *did* run. The 4-second load probe is a burst, not a production soak test. See `SUBMISSION_NOTE.md` for what these tests cannot establish.

## Architecture and safety

Read `DESIGN.md` **before** the code. `app/store.py` implements transaction logic, `app/main.py` implements API/SSE, `app/static/` holds the UI. `tests/test_api.py` tests consistency/isolation/failures, `tests/test_stream_e2e.py` exercises actual HTTP SSE + DB, `tests/test_e2e.py` is actual Chromium + API + DB end-to-end, `scripts/load_probe.py` is a short concurrency pulse.

- **No production deployment implied.** `POST /api/projects` is open to simplify the take-home onboarding. Without rate limiting and abuse protection it must not be exposed to the public internet.
- A key is not a user's identity; this solution models project credentials only. There is no organization membership, login, RBAC, quota enforcement, or key recovery UI.
- The app is configured with one Uvicorn worker, SQLite WAL and no application event queues. SQLite is a simplicity tradeoff, not an assertion that it can sustain production workloads or high availability.

## AI assistance disclosure

AI helped propose the implementation, draft backend/frontend code, tests and documentation. Accepted suggestions: UUID event idempotency, per-run transaction sequence, cursor replay, tenant-scoped SQL, one-time key issuance, real HTTP streaming integration tests, short concurrency probe. These suggestions were **actually checked** by executing the API and SSE tests and examining their unmodified outputs; the browser assertion remains unverified due to the environment restriction. The code and notes have been reviewed for obvious safety limitations, but no independent audit, external deployment, or Docker acceptance run was performed in the authoring environment. See `SUBMISSION_NOTE.md` for the explicit decision and risks.
