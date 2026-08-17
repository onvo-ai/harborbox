"""A representative dashboard-widget workload over the ~100 MB benchmark CSV.

Deliberately shaped like the widget scripts Onvo actually runs: read the file,
reshape it, aggregate a few ways, print a small result. The point of the
benchmark is the *startup* cost around this, so the body is held constant
across every execution path and its own runtime is reported separately.

Reads `BENCH_CSV` from the environment rather than argv so the identical source
text can be run as a script, fed to a Jupyter kernel, or forked by forkrun.
"""

from __future__ import annotations

import json
import os
import time

import pandas as pd

started = time.perf_counter()

frame = pd.read_csv(os.environ["BENCH_CSV"], parse_dates=["ts"])
frame["revenue"] = frame["units"] * frame["unit_price"] * (1 - frame["discount"])
frame["profit"] = frame["revenue"] * frame["margin"]
frame["day"] = frame["ts"].dt.floor("D")

by_region = (
    frame.groupby(["region", "channel"], observed=True)
    .agg(revenue=("revenue", "sum"), profit=("profit", "sum"), orders=("order_id", "count"))
    .sort_values("revenue", ascending=False)
)

won = frame[frame["status"] == "won"]
daily = won.groupby("day", observed=True)["revenue"].sum()
top_customers = won.groupby("customer_id", observed=True)["revenue"].sum().nlargest(10)

print(  # noqa: T201 - stdout is this script's interface
    json.dumps(
        {
            "rows": len(frame),
            "revenue": round(float(frame["revenue"].sum()), 2),
            "segments": len(by_region),
            "best_segment": list(by_region.index[0]),
            "days": len(daily),
            "top_customer": int(top_customers.index[0]),
            "body_seconds": round(time.perf_counter() - started, 4),
        }
    )
)
