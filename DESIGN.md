# Design before code — one-page architecture record

## System and trust boundaries

`browser or metric producer` → `FastAPI (same-origin UI, REST, SSE)` → `SQLite WAL on persistent Docker volume`. All reads, writes and streams authenticate a high-entropy project API key sent in the `X-API-Key` header. Only the key's SHA-256 digest is stored. `POST /api/projects` provides unauthenticated project bootstrap; **this is deliberately a demo limitation** and needs admission controls before exposure to the internet. An untrusted project ID or run ID never grants access.

## Event format and ordering

A request `POST /runs/{id}/metrics` (also `/api/runs/{id}/metrics`) is `{ "batch_id": UUID, "events": [{"event_id": UUID,"name":"accuracy","value":0.94,"observed_at":"2026-11-10T09:00:00Z"}] }`. Limit 250 events per batch, finite numeric values, timezone-aware client timestamps and unique `event_id`s within one batch. Each committed event gets a **monotonic per-run `seq` assigned by the database in write-transaction order**, along with server `received_at`. The `observed_at` timestamp may be earlier than events already committed; it never changes a server sequence. "Latest" in the UI means last *committed*, not last client timestamp. SQLite's `BEGIN IMMEDIATE` serializes writers in one database; `runs.next_seq` and metric inserts commit atomically. A failed batch cannot allocate a permanent gap.

## Duplicate and retry contract

The `(run_id,batch_id)` idempotency record stores a canonical-content digest and the original acknowledgement. Identical batch retry returns the **original response** with no new rows. Reusing `batch_id` for different input yields HTTP 409. Reusing `(run_id,event_id)` in a new batch with **identical** content is an acknowledged duplicate; conflicting payload produces HTTP 409 and rolls back the *entire* batch. `batch_id` and `event_id` are the client's responsibility to keep stable across retransmission. UUIDv4 is required by convention, enforced structurally as UUID, not a particular version.

## Disconnect, catch-up, backpressure

On initial page load, the client retrieves paginated DB events with `after=<seq>`; then it starts SSE with `Last-Event-ID: <last actually applied seq>`. SSE frames contain `id: <seq>`, `event: metric`, and event JSON; the client ignores duplicate sequence numbers. On a transport failure, it reconnects from its last processed cursor. The server polls *durable storage* in ascending pages of 100, instead of putting events into per-client in-memory queues; the streaming sender exerts backpressure. Slow readers may lag, but their events remain in DB, assuming no retention pruning. There is currently no retention, hard client idle timeout, per-subscriber memory budget, snapshot cap, or production traffic proxy tested. A client that maliciously supplies a cursor larger than the committed high-water mark is rejected instead of silently losing data.

## Isolation and leakage

For authenticated endpoints, authorization resolves the project from the key **on every request**, including stream creation. Queries always constrain `project_id` and `run_id`. Cross-project and unknown run IDs both give the same HTTP 404 body. Keys have 256-bit random entropy; UUIDv4 run identifiers are not enumerable in practice, but secrecy of identifiers is not the security boundary. Key rotation revokes the presented key and creates a new key **in one transaction**. No response cache is implemented, private responses set `Cache-Control: no-store`, and the frontend keeps keys only in `sessionStorage` and request headers (never URL). Docker disables access logs. Security gaps: public bootstrap abuse, server-resource/timing side channels, lack of rate limits, multi-process/deployment and proxy behavior untested, no production audit/metrics pipeline. Do not mistake uniform error bodies for constant-time behavior.

## Consistency definition and scale caveat

The authoritative state is the committed `metrics` table ordered by `(run_id,seq)`. After catch-up and network quiescence, the displayed page must equal that database sequence. SQLite WAL with one writer and 250 ms stream polling is a deliberately narrow reference implementation: easy to run, test and reason about, but **not yet a multi-replica production design**. For actual scale, replace storage with PostgreSQL, implement transactional outbox or durable log, rate limits, service metrics and deployment/load tests.
