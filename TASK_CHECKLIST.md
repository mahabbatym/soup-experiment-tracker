# Task → implementation → actual verification

| Assignment requirement | Implemented in | Verified evidence |
|---|---|---|
| Design before code, ≤1 page | `DESIGN_ONE_PAGE.pdf` (+ expanded `DESIGN.md`) | PDF = exactly **1 page** |
| Event ordering and duplicate batches | `app/store.py` | `test_batches_are_idempotent_and_ordered_by_commit_not_client_time`, `test_batch_id_mutation_rejected_and_no_partial_write` |
| Projects, API keys, project-scoped runs | `app/main.py`, `app/store.py` | `test_project_creation_and_auth`, `test_key_rotation_old_key_stops_everywhere_immediately` |
| POST `/runs/{id}/metrics` in batches | `app/main.py` route aliases | `test_exact_assignment_ingest_route_alias` |
| Run list and live run page | `app/static/` | API tests passed; **browser Playwright test blocked by browser policy**, not verified |
| SSE reconnect with no missed stored events | `app/main.py`, `app/static/app.js` | `test_real_http_stream_disconnect_reconnect_and_database_match` passed |
| UI + API + DB E2E incl. duplicates / late / drop | `tests/test_e2e.py` | **Not verified in this environment**; original failure in `evidence/pytest_browser.txt` |
| A cannot read/write/subscribe/guess B, including cache | tenant-scoped SQL and no-store policy | `test_tenant_cannot_read_write_or_subscribe_and_unknown_ids_are_uniform` passed |
| Silent failures: duplicates, overflow, partial writes, key rotation | durable cursor, transactions, key revocation | `tests/test_api.py`, `tests/test_stream_e2e.py` passed; see remaining risks |
| 50 simultaneous runs × 10 events/s evaluation | `scripts/load_probe.py` | 2,000 events stored and streamed in a short local pulse; **not** a production guarantee |
| SHIP / DON'T SHIP with first changes | `SUBMISSION_NOTE.pdf` | `DON'T SHIP` — explicitly explained |
| One/two-page note and AI disclosure | `SUBMISSION_NOTE.pdf` | PDF = exactly **2 pages** |
| Repo, compose, tests and raw output | This archive | Docker unavailable here; Compose syntax parsed, no runtime claim |

**First actions after unpacking:** run `docker compose up`; open `http://localhost:8000`; create and save project key; create a run; generate demo events. To independently reproduce the test evidence, install dev requirements and Playwright Chromium, then run `python -m pytest -vv -ra` and `python scripts/load_probe.py`. Re-run the browser E2E in a browser without managed localhost blocking before including it as passing evidence in any internship submission.
