# Startup and latency benchmarks

The harness behind the numbers quoted in the README and in the PR that removed
the Jupyter kernel. Two independent things are measured, because two
independent things were slow:

- `bench_python_path.py` — what it costs to run Python inside a sandbox
- `bench_queue.py` — what the control plane adds around it

Recorded runs live in `results/`.

## Running them

The Python-path benchmark needs pandas, and needs jupyter-server only to
measure the variant that no longer ships. Use a throwaway venv rather than the
project's, which deliberately no longer has either:

```bash
uv venv /tmp/benchvenv --python 3.12
uv pip install --python /tmp/benchvenv/bin/python \
  pandas==2.2.3 numpy jupyter-server==2.14.2 ipykernel==6.30.1 jupyter-client==8.6.3
/tmp/benchvenv/bin/python -m ipykernel install --sys-prefix --name python3
```

Generate the fixture once (~100 MiB, deterministic), then run both workloads:

```bash
/tmp/benchvenv/bin/python scripts/bench/make_csv.py /tmp/bench.csv

# Trivial body: isolates the execution path's own overhead.
/tmp/benchvenv/bin/python scripts/bench/bench_python_path.py \
  --csv /tmp/bench.csv --script scripts/bench/noop.py --repeats 7

# 100 MB CSV pandas analysis: a realistic widget.
/tmp/benchvenv/bin/python scripts/bench/bench_python_path.py \
  --csv /tmp/bench.csv --script scripts/bench/analysis.py --repeats 5
```

Pass `--skip-jupyter` on a machine without jupyter-server; the remaining
variants still run.

The queue benchmark needs nothing extra:

```bash
uv run python scripts/bench/bench_queue.py --samples 40
```

## What the variants mean

| variant | what it is |
| --- | --- |
| `cold` | `python script.py`. No substrate; pays `import pandas` every run. |
| `jupyter` | What the image used to start: a `jupyter server` plus an ipykernel spawn. |
| `forkrun` | `sandbox/forkrun.py` alone — a daemon that imports pandas once and forks per run. |

The recorded runs in `results/` also carry a fourth variant, `coderun`. That
was `sandbox/coderun.py`, the runner written to serve
`POST /v1/sandboxes/{id}/executions` after the kernel was removed. The endpoint
turned out to have no caller and was deleted along with the runner, so the
harness no longer produces that variant; the results keep it because it is what
was measured at the time.

`cold_start` is substrate boot plus first execution — what a caller waits for on
a sandbox that just started. `warm` is the median of the runs after that.
`substrate_rss` is memory held before any user code runs.

## Reading the results honestly

Three caveats travel with these numbers.

**Jupyter's `warm` column is genuinely faster, and it should not decide the
question.** On the trivial workload it is ~50 ms ahead; on the CSV workload
~0.8 s ahead. Both come from the kernel keeping one namespace across runs — the
allocator stays warm and the previous run's DataFrame is still resident, which
is why `substrate_rss` reads ~578 MB after the CSV run rather than ~197 MB. That
residency is exactly the cross-execution bleed the product forbids for
customer-authored widget code (see `forkrun.py`'s module docstring). The
comparison to trust is `cold_start` and `substrate_rss`.

**The machine matters.** These were taken on 4 CPUs / 16 GB. Sandbox templates
are sized at 0.5–2 CPU, so Jupyter's boot in a real sandbox is worse than what
is recorded here, not better. A prior comment in the codebase put it at 6–11 s;
this harness measures ~3 s on a more generous host. The direction is the same
and the magnitude is not settled — treat ~3 s as a floor.

**The queue benchmark models the waiting, not the database.** Both of its modes
run the real `ExecutionNotifier` and the real `Settings` intervals, with a stub
in place of the sandbox, so it isolates the queue's own contribution. Database
round trips are identical in both modes and cancel out. It is not an end-to-end
number.

One thing here is not measured at all: the pause ladder
(`running → paused_memory → paused_cold`) needs a container runtime to freeze
and unfreeze, and there was no Docker daemon available. Its unit tests pin the
decision logic; the latency claim behind it is reasoned from how Docker's
freezer works, not measured.
