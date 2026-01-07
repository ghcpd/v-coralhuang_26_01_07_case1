"""
Optimized audit log service (NEW)

- Builds an in-memory ordered index of event metadata (id, created_at, ...,
  payload_offset, payload_len) at init time.
- Supports the same public semantics as the baseline (ordering, fields).
- Satisfies the payload read contract: for every returned event we perform a
  real file.seek(offset) + file.read(len) and JSON-decode the bytes. Payload
  bytes or decoded payloads are NOT cached.
- Thread-safe and designed for high-concurrency reads.

This module is additive only — it does not modify baseline files.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

_init_lock = threading.Lock()
_inited = False

# in-memory structures (populated at init)
_index: List[Dict[str, Any]] = []
_actor_index: Dict[int, List[int]] = {}
_action_index: Dict[str, List[int]] = {}

# instrumentation (for tests)
_instrument_lock = threading.Lock()
_seek_read_counters: Dict[int, Dict[str, int]] = {}


@dataclass(frozen=True)
class Query:
    from_ts: int
    to_ts: int
    actor_id: Optional[int]
    action: Optional[str]
    page: int
    page_size: int


def init() -> None:
    """Populate in-memory ordered index (id order: created_at DESC, id DESC)."""
    global _inited, _index, _actor_index, _action_index
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, created_at, actor_id, action, resource_type, resource_id,
                   payload_offset, payload_len
            FROM audit_events
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cur.fetchall()
        conn.close()

        idx: List[Dict[str, Any]] = []
        actor_index: Dict[int, List[int]] = {}
        action_index: Dict[str, List[int]] = {}

        for pos, (eid, created_at, actor_id, action, rtype, rid, poff, plen) in enumerate(rows):
            entry = {
                "id": eid,
                "created_at": created_at,
                "actor_id": actor_id,
                "action": action,
                "resource_type": rtype,
                "resource_id": rid,
                "payload_offset": poff,
                "payload_len": plen,
            }
            idx.append(entry)

            actor_index.setdefault(actor_id, []).append(pos)
            action_index.setdefault(action, []).append(pos)

        # publish
        _index = idx
        _actor_index = actor_index
        _action_index = action_index
        _inited = True


# ------------------------- payload read (must seek+read per event) -------------------------
def _read_payload_seek_read(offset: int, length: int) -> Any:
    """Open payload file, perform f.seek + f.read(length), json.loads and
    return the decoded object. Also increments instrumentation counters for the
    calling thread.
    """
    tid = threading.get_ident()
    with open(PAYLOAD_FILE, "rb") as f:
        # instrumentation: record that this thread did a seek/read
        f.seek(offset)
        data = f.read(length)
    # record after successful read to avoid counting failed attempts
    with _instrument_lock:
        c = _seek_read_counters.setdefault(tid, {"seek": 0, "read": 0})
        c["seek"] += 1
        c["read"] += 1
    return json.loads(data)


def get_and_reset_instrumentation() -> Dict[str, int]:
    """Collect and reset global instrumentation counters (aggregate across threads)."""
    with _instrument_lock:
        total_seek = sum(c.get("seek", 0) for c in _seek_read_counters.values())
        total_read = sum(c.get("read", 0) for c in _seek_read_counters.values())
        _seek_read_counters.clear()
    return {"seek": total_seek, "read": total_read}


# ------------------------- query execution -------------------------
def _filter_positions(q: Query) -> List[int]:
    """Return ordered list of positions in _index that match the time + filter
    criteria. Implements efficient intersections when actor_id/action are set.
    """
    # time window filter: since _index is ordered by created_at DESC, id DESC,
    # perform a linear scan to collect positions within [from_ts, to_ts]. This
    # is fast for 50k rows and simpler than maintaining a complex tree.
    res: List[int] = []
    idx = _index
    if not q.actor_id and not q.action:
        # fast path: only time filter
        for pos, e in enumerate(idx):
            if q.from_ts <= e["created_at"] <= q.to_ts:
                res.append(pos)
        return res

    # build candidate lists from actor/action indices then apply time window
    candidates: Optional[List[int]] = None
    if q.actor_id:
        candidates = _actor_index.get(q.actor_id, [])
    if q.action:
        a_list = _action_index.get(q.action, [])
        if candidates is None:
            candidates = a_list
        else:
            # intersect two ordered lists (both ordered by the same global order)
            i = j = 0
            out: List[int] = []
            while i < len(candidates) and j < len(a_list):
                if candidates[i] == a_list[j]:
                    out.append(candidates[i])
                    i += 1
                    j += 1
                elif candidates[i] < a_list[j]:
                    i += 1
                else:
                    j += 1
            candidates = out
    if candidates is None:
        candidates = list(range(len(idx)))

    for pos in candidates:
        e = idx[pos]
        if q.from_ts <= e["created_at"] <= q.to_ts:
            res.append(pos)
    return res


def query(q: Query) -> Dict[str, Any]:
    """Deterministic query API — returns the same structure as the baseline.

    This function is used by the test harness to compare results exactly.
    """
    init()
    positions = _filter_positions(q)
    offset_rows = (q.page - 1) * q.page_size
    slice_positions = positions[offset_rows : offset_rows + q.page_size]

    events: List[Dict[str, Any]] = []
    # open the payload file once per query and perform seek+read per event
    with open(PAYLOAD_FILE, "rb") as f:
        for pos in slice_positions:
            e = _index[pos]
            f.seek(e["payload_offset"])
            data = f.read(e["payload_len"])
            payload = json.loads(data)
            events.append(
                {
                    "id": e["id"],
                    "created_at": e["created_at"],
                    "actor_id": e["actor_id"],
                    "action": e["action"],
                    "resource_type": e["resource_type"],
                    "resource_id": e["resource_id"],
                    "payload": payload,
                }
            )
    return {"query": q.__dict__, "events": events, "count": len(events)}


# compatibility entrypoint for benchmarking (mimics baseline handle_request)
import random
_rng = random.Random(1337)


def _pick_query() -> Query:
    now = int(time.time())
    return Query(
        from_ts=now - 7 * 24 * 3600,
        to_ts=now,
        actor_id=_rng.choice([None, _rng.randint(1, 2000)]),
        action=_rng.choice([None, "UPDATE", "CREATE", "DELETE"]),
        page=_rng.randint(1, 2000),
        page_size=_rng.choice([10, 20, 50]),
    )


def handle_request() -> Dict[str, Any]:
    q = _pick_query()
    return query(q)
