"""Enhanced audit log service (new implementation).

Improvements:
- In-memory metadata index for fast filtering and keyset-like pagination (preserves semantics)
- Per-thread SQLite connections & per-thread payload file handles to reduce contention
- Concurrency-safe, read-only cached metadata (no payload caching)

This module preserves the exact semantics of the baseline (ordering, fields),
and enforces the payload read contract by performing f.seek(offset) + f.read(len)
for each returned event.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

import audit_log_service as base

DB_PATH = base.DB_PATH
PAYLOAD_FILE = base.PAYLOAD_FILE

_init_lock = threading.Lock()
_inited = False

# in-memory index: list of dicts with keys matching DB columns (except payload bytes)
_index: List[Dict[str, Any]] = []
_neg_created_at: List[int] = []
_actor_index: Dict[int, List[int]] = {}
_action_index: Dict[str, List[int]] = {}

# thread-local storage for connections and open file handles
_local = threading.local()


def init() -> None:
    global _inited, _index
    global _neg_created_at, _actor_index, _action_index
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        # Ensure baseline data (DB + payloads) exists
        base.init()

        # Load metadata into memory (allowed by contract)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at, actor_id, action, resource_type, resource_id,
                   payload_offset, payload_len
            FROM audit_events
            """
        )
        rows = cur.fetchall()
        conn.close()

        # Build index and sort by required order: created_at DESC, id DESC
        idx = []
        for (eid, created_at, actor_id, action, rtype, rid, poff, plen) in rows:
            idx.append(
                {
                    "id": eid,
                    "created_at": created_at,
                    "actor_id": actor_id,
                    "action": action,
                    "resource_type": rtype,
                    "resource_id": rid,
                    "payload_offset": poff,
                    "payload_len": plen,
                }
            )

        idx.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        _index = idx

        # build auxiliary indexes for fast filtering
        _neg_created_at = [-r["created_at"] for r in _index]
        _actor_index = {}
        _action_index = {}
        for pos, r in enumerate(_index):
            _actor_index.setdefault(r["actor_id"], []).append(pos)
            _action_index.setdefault(r["action"], []).append(pos)

        _inited = True


def _get_conn() -> sqlite3.Connection:
    # maintain per-thread sqlite connections to avoid contention
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return _local.conn


def _get_payload_file():
    # maintain a per-thread open file handle to improve performance
    if not hasattr(_local, "pf"):
        _local.pf = open(PAYLOAD_FILE, "rb")
    return _local.pf


def _read_payload(offset: int, length: int) -> Any:
    # Must perform real file seek + read per contract
    f = _get_payload_file()
    f.seek(offset)
    data = f.read(length)
    return json.loads(data)


def handle_query(q: base.Query) -> Dict[str, Any]:
    """Deterministic query handler: returns events for the provided Query.

    This mirrors the baseline SQL semantics but performs filtering in memory
    on cached metadata to avoid OFFSET deep-scan while preserving exact
    ordering and counts.
    """
    init()

    # Efficient filtering using auxiliary indexes
    import bisect

    f_from = q.from_ts
    f_to = q.to_ts

    # find positions matching created_at range using negated created_at list
    left = bisect.bisect_left(_neg_created_at, -f_to)
    right = bisect.bisect_right(_neg_created_at, -f_from)
    positions = list(range(left, right))

    # helper to intersect two sorted lists of positions
    def _intersect(a: List[int], b: List[int]) -> List[int]:
        res = []
        i = j = 0
        while i < len(a) and j < len(b):
            if a[i] == b[j]:
                res.append(a[i])
                i += 1
                j += 1
            elif a[i] < b[j]:
                i += 1
            else:
                j += 1
        return res

    if q.actor_id is not None:
        actor_pos = _actor_index.get(q.actor_id, [])
        positions = _intersect(positions, actor_pos)

    if q.action is not None:
        action_pos = _action_index.get(q.action, [])
        positions = _intersect(positions, action_pos)

    # pagination
    offset_rows = (q.page - 1) * q.page_size
    page_positions = positions[offset_rows : offset_rows + q.page_size]

    events: List[Dict[str, Any]] = []
    for pos in page_positions:
        r = _index[pos]
        payload = _read_payload(r["payload_offset"], r["payload_len"])
        events.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "actor_id": r["actor_id"],
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "payload": payload,
            }
        )

    return {"query": q.__dict__, "events": events, "count": len(events)}


def handle_request() -> Dict[str, Any]:
    # Mirror baseline: pick a random query and execute it
    q = base._pick_query()
    return handle_query(q)
