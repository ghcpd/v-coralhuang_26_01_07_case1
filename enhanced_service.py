"""
Enhanced audit log service — non-invasive improvement over the baseline.

Strategy (high level):
- Build an in-memory, read-only index of the audit_events table (metadata only).
- Use fast binary-search over the timestamp-ordered index to narrow the candidate slice
  for a time window, then apply actor/action filters in Python and page the results.
- Keep payload reading semantics identical: each returned event performs a real
  file.seek(...) + file.read(...) and json.loads(...) (no payload caching).
- Index is built once (on init) and is safe for concurrent reads.

This module exposes:
- EnhancedAuditLogService.init()
- EnhancedAuditLogService.query(q) -> dict (same shape as baseline handle_request)

This file only uses the Python standard library and does not modify baseline files.
"""
from __future__ import annotations

import bisect
import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from audit_log_service import DB_PATH, PAYLOAD_FILE, Query, init as baseline_init


class EnhancedAuditLogService:
    """In-memory indexed, concurrency-friendly wrapper around the immutable dataset.

    The index stores only metadata (no payload bytes). For each query we open the
    payload file and perform a real seek+read per returned event (payload bytes are
    not cached).
    """

    _build_lock = threading.Lock()

    def __init__(self) -> None:
        self._indexed = False
        self._index: List[Tuple[int, int, int, str, str, str, int, int]] = []
        # _keys holds tuples used for bisect: (-created_at, -id)
        self._keys: List[Tuple[int, int]] = []

    def init(self) -> None:
        if self._indexed:
            return
        with self._build_lock:
            if self._indexed:
                return
            # Ensure baseline dataset exists
            baseline_init()

            conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            cur = conn.cursor()
            # Read all rows ordered exactly as the baseline's ORDER BY
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

            # Store rows as-is. Each row is a tuple matching the SELECT order.
            # Build keys for fast time-window bisecting. We negate values so we can
            # use bisect on an effectively ascending list that represents the
            # desired descending order.
            self._index = [
                (
                    int(r[0]),
                    int(r[1]),
                    int(r[2]),
                    str(r[3]),
                    str(r[4]),
                    str(r[5]),
                    int(r[6]),
                    int(r[7]),
                )
                for r in rows
            ]
            self._keys = [(-r[1], -r[0]) for r in self._index]
            self._indexed = True

    def _read_payloads(self, rows: Sequence[Tuple[int, int, int, str, str, str, int, int]]) -> List[Any]:
        events: List[Any] = []
        # Open payloads file once per request, but perform seek/read per event
        with open(PAYLOAD_FILE, "rb") as f:
            for (
                eid,
                created_at,
                actor_id,
                action,
                rtype,
                rid,
                poff,
                plen,
            ) in rows:
                f.seek(poff)
                payload = json.loads(f.read(plen))
                events.append(
                    {
                        "id": eid,
                        "created_at": created_at,
                        "actor_id": actor_id,
                        "action": action,
                        "resource_type": rtype,
                        "resource_id": rid,
                        "payload": payload,
                    }
                )
        return events

    def query(self, q: Query) -> Dict[str, Any]:
        """Return results for a given Query object (semantics preserved).

        The function mirrors the baseline semantics exactly:
        - Apply created_at BETWEEN from_ts AND to_ts
        - Apply actor_id and action filters when not None
        - Ordering: created_at DESC, id DESC
        - Pagination: LIMIT page_size OFFSET (page-1)*page_size

        The only difference is that the result set is produced from an in-memory
        index (metadata only) which avoids per-request SQL LIMIT/OFFSET overhead.
        Payload bytes are still read from disk per returned event.
        """
        if not self._indexed:
            raise RuntimeError("Service not initialized. Call init() first.")

        # Find the contiguous slice of rows whose created_at falls in the time window
        # using bisect on self._keys (which represent (-created_at, -id)).
        left = bisect.bisect_left(self._keys, (-q.to_ts, float("-inf")))
        right = bisect.bisect_right(self._keys, (-q.from_ts, float("inf")))

        candidates = self._index[left:right]

        # Apply actor/action filters (when provided)
        if q.actor_id is not None:
            candidates = [r for r in candidates if r[2] == q.actor_id]
        if q.action is not None:
            candidates = [r for r in candidates if r[3] == q.action]

        # Pagination
        offset_rows = (q.page - 1) * q.page_size
        page_slice = candidates[offset_rows : offset_rows + q.page_size]

        events = self._read_payloads(page_slice)

        return {"query": q.__dict__, "events": events, "count": len(events)}


# Module-level convenience instance used by the tests/runner
_service = EnhancedAuditLogService()


def init() -> None:
    _service.init()


def query(q: Query) -> Dict[str, Any]:
    return _service.query(q)
