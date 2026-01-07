# enhanced_audit_log_service.py
"""
Enhanced: Audit log list endpoint with improved efficiency and robustness.

Enhancements:
- Pre-load all event metadata into memory for fast filtering and pagination.
- Avoid DB connection churn per request.
- Maintain semantic equivalence: same sort order, fields, payload reads from disk.
"""

from __future__ import annotations

import bisect
import json
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Import baseline for seeding
import audit_log_service

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

_rng = random.Random(1337)

MAX_PAGE = 2000

@dataclass(frozen=True)
class Query:
    from_ts: int
    to_ts: int
    actor_id: Optional[int]
    action: Optional[str]
    page: int
    page_size: int

_init_lock = threading.Lock()
_inited = False
_all_events: List[Dict[str, Any]] = []
_created_ats: List[int] = []

def init() -> None:
    global _inited, _all_events
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        audit_log_service.init()  # Seed if needed
        _load_events()
        _inited = True

def _load_events() -> None:
    global _all_events
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
    _all_events = [
        {
            "id": row[0],
            "created_at": row[1],
            "actor_id": row[2],
            "action": row[3],
            "resource_type": row[4],
            "resource_id": row[5],
            "payload_offset": row[6],
            "payload_len": row[7],
        }
        for row in rows
    ]
    global _created_ats
    _created_ats = [row[1] for row in rows]

def _pick_query():
    # Same as baseline
    now = int(time.time())
    return Query(
        from_ts=now - 7 * 24 * 3600,
        to_ts=now,
        actor_id=_rng.choice([None, _rng.randint(1, 2000)]),
        action=_rng.choice([None, "UPDATE", "CREATE", "DELETE"]),
        page=_rng.randint(1, MAX_PAGE),
        page_size=_rng.choice([10, 20, 50]),
    )

def _read_payload(offset: int, length: int) -> Any:
    # Same as baseline
    with open(PAYLOAD_FILE, "rb") as f:
        f.seek(offset)
        return json.loads(f.read(length))

def handle_request() -> Dict[str, Any]:
    q = _pick_query()
    # Filter by time using bisect
    negated = [-ts for ts in _created_ats]
    start_idx = bisect.bisect_left(negated, -q.to_ts)
    end_idx = bisect.bisect_right(negated, -q.from_ts)
    time_filtered = _all_events[start_idx:end_idx]
    # Filter by actor_id and action
    filtered = [
        e for e in time_filtered
        if (q.actor_id is None or e["actor_id"] == q.actor_id)
        and (q.action is None or e["action"] == q.action)
    ]
    # Paginate
    offset_rows = (q.page - 1) * q.page_size
    page_events = filtered[offset_rows: offset_rows + q.page_size]
    # Read payloads
    events: List[Dict[str, Any]] = []
    for e in page_events:
        payload = _read_payload(e["payload_offset"], e["payload_len"])
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
    return {
        "query": q.__dict__,
        "events": events,
        "count": len(events),
    }