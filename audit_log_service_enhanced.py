# audit_log_service_enhanced.py
"""
Enhanced Audit Log Service with SQLite optimizations and connection pooling.

This module patches the baseline audit_log_service module to apply:
1. SQLite PRAGMA optimizations (WAL, larger cache, mmap I/O)
2. Thread-local connection reuse (avoids per-request connection churn)
3. Zero modification to baseline files or semantics

Optimizations:
- WAL mode for concurrent read support
- Memory-mapped I/O for faster data access
- Larger query cache for hot data
- Thread-local connection reuse (lock-free)
- Memory-backed temp tables

All semantic requirements preserved:
- Sort order: created_at DESC, id DESC
- Required fields per event: id, created_at, actor_id, action, resource_type, resource_id, payload
- Payload must be read from disk for each event (no caching)
- Query generation unchanged (uses baseline's _pick_query())
- Result structure unchanged
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Dict, List

DB_PATH = "audit.db"
PAYLOAD_FILE = "payloads.jsonl"

# Thread-local storage for per-thread connections (avoids lock contention)
_thread_local = threading.local()

# Track whether we've patched the baseline
_patched = False


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply SQLite optimizations for read-heavy workload."""
    # Enable WAL mode for better concurrency (readers don't block writers)
    conn.execute("PRAGMA journal_mode=WAL")
    
    # Increase cache size (default is 2000 pages, 8KB each)
    conn.execute("PRAGMA cache_size=10000")
    
    # Use memory-mapped I/O for faster access (up to 128MB)
    conn.execute("PRAGMA mmap_size=134217728")
    
    # Reduce synchronous writes since this is read-heavy
    conn.execute("PRAGMA synchronous=NORMAL")
    
    # Use memory for temp tables (not disk)
    conn.execute("PRAGMA temp_store=MEMORY")


def _get_thread_conn() -> sqlite3.Connection:
    """Get the current thread's connection (create if needed, with optimizations)."""
    if not hasattr(_thread_local, 'conn'):
        _thread_local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _configure_connection(_thread_local.conn)
    return _thread_local.conn


def _patch_baseline() -> None:
    """
    Monkey-patch the baseline module to use thread-local connections.
    This preserves all semantics while improving efficiency.
    """
    global _patched
    if _patched:
        return
    
    import audit_log_service as baseline
    
    # Create wrapped version that uses thread-local connections
    def pooled_handle_request() -> Dict[str, Any]:
        """Enhanced handle_request using thread-local connection reuse."""
        q = baseline._pick_query()
        offset_rows = (q.page - 1) * q.page_size
        
        # Use thread-local connection instead of per-request sqlite3.connect()
        conn = _get_thread_conn()
        cur = conn.cursor()
        
        # Same query logic as baseline
        cur.execute(
            """
            SELECT id, created_at, actor_id, action, resource_type, resource_id,
                   payload_offset, payload_len
            FROM audit_events
            WHERE created_at BETWEEN ? AND ?
              AND (? IS NULL OR actor_id = ?)
              AND (? IS NULL OR action = ?)
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (
                q.from_ts,
                q.to_ts,
                q.actor_id,
                q.actor_id,
                q.action,
                q.action,
                q.page_size,
                offset_rows,
            ),
        )
        rows = cur.fetchall()
        
        # Same payload reading as baseline (disk seek + read per event)
        events: List[Dict[str, Any]] = []
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
            payload = baseline._read_payload(poff, plen)
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
        
        return {
            "query": q.__dict__,
            "events": events,
            "count": len(events),
        }
    
    # Replace baseline's handle_request with our optimized version
    baseline.handle_request = pooled_handle_request
    _patched = True


def init() -> None:
    """Initialize baseline and apply connection optimization."""
    import audit_log_service
    audit_log_service.init()
    _patch_baseline()


def handle_request() -> Dict[str, Any]:
    """
    Delegate to baseline (which is now using thread-local connections).
    This function exists for compatibility.
    """
    import audit_log_service as baseline
    return baseline.handle_request()


def cleanup() -> None:
    """Clean up thread-local connections."""
    if hasattr(_thread_local, 'conn'):
        try:
            _thread_local.conn.close()
        except:
            pass
        delattr(_thread_local, 'conn')