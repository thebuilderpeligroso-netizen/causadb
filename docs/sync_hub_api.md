# Sync Hub API

This document defines the REST API contract that any hub server must implement
for the CausaDB ledger federation (hub-and-spoke sync).

> **Ledger is append-only** — sync is trivial: push new events from spoke to hub,
> pull missing events from hub to spoke. No CRDT needed.

---

## POST /sync/push

Accept new events from a spoke node.

**Request**

- Method: `POST`
- Headers:
  - `Content-Type: application/json`
  - `X-API-Key: <node-api-key>`
- Body:

```json
{
  "events": [
    {
      "event": { ... },
      "prev_hash": "abc123",
      "hash": "def456"
    }
  ],
  "last_seq": 42,
  "node_id": "node-a1b2c3d4e5f6"
}
```

| Field       | Type   | Description                             |
|-------------|--------|-----------------------------------------|
| `events`    | array  | Ledger entries (each with `event`, `prev_hash`, `hash`) |
| `last_seq`  | int    | The node's last synced sequence number  |
| `node_id`   | string | Stable node identifier                  |

**Response** (200)

```json
{
  "accepted": 5,
  "new_last_seq": 47
}
```

| Field          | Type | Description                              |
|----------------|------|------------------------------------------|
| `accepted`     | int  | Number of events accepted                |
| `new_last_seq` | int  | New max sequence number on the hub       |

---

## GET /sync/pull

Return events after sequence N that the requesting node does not have.

**Request**

- Method: `GET`
- Headers:
  - `X-API-Key: <node-api-key>`
- Query parameters:

| Parameter | Type   | Description                              |
|-----------|--------|------------------------------------------|
| `last_seq` | int   | Return events with sequence_number > this value |
| `node_id`  | string| Stable node identifier                   |

**Response** (200)

```json
{
  "events": [
    {
      "event": { ... },
      "prev_hash": "abc123",
      "hash": "def456"
    }
  ],
  "last_seq": 47
}
```

| Field      | Type   | Description                              |
|------------|--------|------------------------------------------|
| `events`   | array  | Ledger entries (may be empty)            |
| `last_seq` | int    | Max sequence number in the returned set  |

If no new events are available, return an empty `events` array and the current
hub `last_seq`.

---

## Error responses

All endpoints return JSON on failure:

```json
{
  "error": "description of what went wrong"
}
```

| HTTP status | Meaning                          |
|-------------|----------------------------------|
| 400         | Bad request (missing fields)     |
| 401         | Missing or invalid `X-API-Key`   |
| 403         | Valid key but insufficient permissions |
| 500         | Internal hub error               |

---

## Authentication

All endpoints require the `X-API-Key` header. The hub validates the key against
its user store (CausaDB RBAC, #10). The key must have at least `member` role
to push/pull events.

---

## Implementation notes for hub operators

- The hub **must** preserve the `prev_hash` → `hash` chain continuity when
  accepting events from multiple nodes. The simplest approach is to maintain an
  append-only ledger on the hub and assign monotonic sequence numbers.
- Nodes identify themselves via `node_id` — the hub may track per-node
  `last_synced_seq` for pull filtering.
- A minimal hub can be implemented as a thin REST wrapper around a CausaDB
  instance using the existing `LedgerWriter.append()` and `LedgerReader`.
- For production, the hub can be backed by Supabase/Postgres or any storage
  that supports append-only event streams.
