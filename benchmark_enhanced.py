#!/usr/bin/env python3
"""
Benchmark enhanced version - runs in subprocess.
"""

import json
import audit_log_service_enhanced as enhanced
import runner

enhanced.init()  # This patches baseline

import audit_log_service as baseline

def enhanced_load():
    import time
    t0 = time.perf_counter()
    baseline.handle_request()
    t1 = time.perf_counter()
    return t0, t1

metrics, _ = runner.run(enhanced_load, warmup=1, total=50, concurrency=32)
print(json.dumps(metrics))
