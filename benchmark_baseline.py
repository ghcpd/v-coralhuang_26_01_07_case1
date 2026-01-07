#!/usr/bin/env python3
"""
Benchmark original (unpatched) baseline - runs in subprocess to avoid monkey-patching interference.
"""

import json
import audit_log_service as baseline
import runner

baseline.init()

def baseline_load():
    import time
    t0 = time.perf_counter()
    baseline.handle_request()
    t1 = time.perf_counter()
    return t0, t1

metrics, _ = runner.run(baseline_load, warmup=1, total=50, concurrency=32)
print(json.dumps(metrics))
