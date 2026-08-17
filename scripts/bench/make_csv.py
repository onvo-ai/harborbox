"""Generate the ~100 MB CSV the startup benchmarks read.

Deterministic on purpose: every variant in `bench_python_path.py` must read
byte-identical input, or the comparison measures the fixture rather than the
execution path. The seed is fixed and the row count is chosen so the file lands
just over 100 MB with the column set below.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROWS = 1_400_000
SEED = 20260817

REGIONS = ("emea", "apac", "amer", "latam")
CHANNELS = ("web", "mobile", "partner", "field", "reseller")
STATUSES = ("open", "won", "lost", "pending")


def build(rows: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "order_id": np.arange(rows, dtype=np.int64),
            "ts": pd.date_range("2024-01-01", periods=rows, freq="s"),
            "region": rng.choice(REGIONS, rows),
            "channel": rng.choice(CHANNELS, rows),
            "status": rng.choice(STATUSES, rows),
            "customer_id": rng.integers(1, 90_000, rows, dtype=np.int64),
            "units": rng.integers(1, 250, rows, dtype=np.int64),
            "unit_price": np.round(rng.uniform(4.0, 900.0, rows), 2),
            "discount": np.round(rng.uniform(0.0, 0.45, rows), 4),
            "margin": np.round(rng.uniform(-0.2, 0.6, rows), 4),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--seed", type=int, default=SEED)
    arguments = parser.parse_args()

    if arguments.destination.exists():
        print(f"reusing {arguments.destination}")  # noqa: T201 - CLI output
        return 0

    arguments.destination.parent.mkdir(parents=True, exist_ok=True)
    build(arguments.rows, arguments.seed).to_csv(arguments.destination, index=False)
    size_mb = arguments.destination.stat().st_size / (1024 * 1024)
    print(f"wrote {arguments.destination} ({size_mb:.1f} MiB)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
