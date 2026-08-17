"""The smallest useful widget: isolates per-execution overhead from body cost.

`analysis.py` spends ~4 s reading a 100 MB CSV, which swamps the differences
between execution paths. This script does a trivial pandas operation instead,
so what the benchmark measures is almost entirely the path's own overhead --
which is the number that decides whether a sub-second round trip is reachable.
"""

from __future__ import annotations

import json
import time

import pandas as pd

started = time.perf_counter()
frame = pd.DataFrame({"a": range(1000), "b": range(1000)})
total = int((frame["a"] * frame["b"]).sum())

print(  # noqa: T201 - stdout is this script's interface
    json.dumps({"total": total, "body_seconds": round(time.perf_counter() - started, 4)})
)
