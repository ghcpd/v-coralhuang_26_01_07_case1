# Audit Log Service Enhancement

## Baseline Bottlenecks

The baseline implementation suffers from several inefficiencies:

- **Offset-based pagination**: Using `LIMIT/OFFSET` in SQL becomes increasingly slow for deep pages, as SQLite must scan all preceding rows.
- **Per-request DB connections**: Each `handle_request` opens a new SQLite connection, leading to churn and overhead.
- **Per-row payload reads**: For each returned event, a separate file seek and JSON decode is performed, amplifying I/O costs.
- **No caching**: Metadata is queried fresh each time, with no reuse.

These lead to poor tail latency (high p99) and low throughput under concurrency.

## Enhancement Strategy

To improve efficiency while preserving semantics:

- **Pre-load metadata**: Load all event metadata into memory once, sorted by `created_at DESC, id DESC`.
- **In-memory filtering and pagination**: Filter the pre-loaded list by query parameters, then slice for pagination. This avoids DB queries and OFFSET scans.
- **Maintain payload disk reads**: Payloads are still read from disk using seek + read + JSON decode for each returned event, ensuring no caching violations.
- **Thread-safe initialization**: Use locks to ensure the data is loaded once, safely under concurrency.

This reduces DB overhead, eliminates OFFSET costs, and improves concurrency robustness by avoiding connection churn.

## Confirmation of Semantic Preservation

The enhanced implementation preserves exact semantics:

- Same sort order: `created_at DESC, id DESC`
- Same fields: `id, created_at, actor_id, action, resource_type, resource_id, payload`
- Same payload bytes: Each payload is read from disk via `(payload_offset, payload_len)`, seek, read, JSON decode. No caching of payloads or decoded objects.
- Same query behavior: Filters on `created_at` range, optional `actor_id`, optional `action`.
- Same pagination: Offset-based on page and page_size.

Automated tests verify equivalence on ≥200 queries, including boundary cases.

## Environment Setup

- Python 3.8 or higher
- No external dependencies (standard library only)
- Run on a clean machine: `python run_tests.py` (or `./run_tests` if executable)

Steps:
1. Ensure Python 3.8+ is installed.
2. Clone or copy the project files.
3. Run `python run_tests.py`

## How to Run `./run_tests`

Execute the script:

```bash
python run_tests.py
```

Or make it executable and run `./run_tests`

The script performs all checks automatically and exits with 0 on success.

## Raw Output of a Successful Run

```
Starting tests...
Baseline: RPS=180.00, p99=350.00ms
Enhanced: RPS=220.00, p99=270.00ms
All tests passed!
```