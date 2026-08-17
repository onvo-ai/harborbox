"""Measure what each Python execution path costs for one widget-shaped workload.

Four variants, all running byte-identical source over the same ~100 MB CSV, so
the only difference is the machinery around the code:

* `cold`    - `python analysis.py`. No substrate, but every run pays the
              interpreter boot and the ~1.5 s `import pandas`.
* `jupyter` - what the sandbox image used to start: a `jupyter server` process,
              plus an ipykernel spawn, before any code runs.
* `forkrun` - `sandbox/forkrun.py`: a daemon that imports pandas once and forks
              a pristine child per run.
* `coderun` - forkrun *and* `sandbox/coderun.py`. What `POST /v1/executions`
              runs today, and so the variant to compare against `jupyter`.

Two numbers matter and they are reported separately:

* `cold_start` - substrate boot + first execution. What a caller waits for when
                 the sandbox has just started. This is the number the Jupyter
                 removal is aimed at.
* `warm`       - median of the repeated executions after that. What a caller
                 waits for on a sandbox that is already up.

The `jupyter` variant is measured as (server boot) + (kernel spawn + execute)
rather than by driving the server's websocket protocol. Those are the same two
costs the sandbox pays -- execd polls the server's `/api/kernelspecs` and then
runs code on a kernel the server spawns -- and decomposing them avoids
reimplementing the wire protocol just to time it. The decomposition is
generous to Jupyter: it omits the server's own websocket and routing overhead.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
SANDBOX_DIR = BENCH_DIR.parent.parent / "sandbox"
FORKRUN = SANDBOX_DIR / "forkrun.py"
CODERUN = SANDBOX_DIR / "coderun.py"
FORKRUN_SOCKET = Path("/tmp/.harborbox-forkrun.sock")  # noqa: S108 - forkrun's fixed path
JUPYTER_PORT = 8899
BOOT_TIMEOUT_S = 120.0
HTTP_OK = 200


@dataclass
class Variant:
    name: str
    cold_start_seconds: float
    boot_seconds: float
    first_execution_seconds: float
    warm_seconds: float
    warm_samples: list[float]
    body_seconds: float
    substrate_rss_mb: float = 0.0
    note: str = ""


def _environment(csv_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["BENCH_CSV"] = str(csv_path)
    return environment


def _body_seconds(stdout: str) -> float:
    """Pull the analysis body's self-timing out of its JSON result line."""
    for line in reversed(stdout.strip().splitlines()):
        with suppress(json.JSONDecodeError):
            return float(json.loads(line)["body_seconds"])
    message = f"analysis produced no parsable result: {stdout[-400:]!r}"
    raise RuntimeError(message)


def _run(command: list[str], environment: dict[str, str]) -> tuple[float, str]:
    started = time.perf_counter()
    completed = subprocess.run(  # noqa: S603 - fixed argv built in this module
        command, capture_output=True, text=True, env=environment, check=False
    )
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        message = f"{command[:2]} failed ({completed.returncode}): {completed.stderr[-800:]}"
        raise RuntimeError(message)
    return elapsed, completed.stdout


def _rss_mb(pid: int) -> float:
    """Resident memory of one process, from procfs. 0.0 if it has already gone."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return 0.0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _tree_rss_mb(pid: int) -> float:
    """Resident memory of a process and its children, which is what a sandbox pays."""
    total = _rss_mb(pid)
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return total
    return total + sum(_rss_mb(int(child)) for child in children.split())


def _kernel_rss_mb(manager: object) -> float:
    """Resident memory of the spawned ipykernel, however this jupyter_client exposes it."""
    provisioner = getattr(manager, "provisioner", None)
    process = getattr(provisioner, "process", None)
    pid = getattr(process, "pid", None)
    return _rss_mb(int(pid)) if pid else 0.0


def bench_cold(python: str, script: Path, csv_path: Path, repeats: int) -> Variant:
    environment = _environment(csv_path)
    first, stdout = _run([python, str(script)], environment)
    samples = [_run([python, str(script)], environment)[0] for _ in range(repeats)]
    return Variant(
        name="cold",
        cold_start_seconds=first,
        boot_seconds=0.0,
        first_execution_seconds=first,
        warm_seconds=statistics.median(samples),
        warm_samples=samples,
        body_seconds=_body_seconds(stdout),
        note="no substrate; every run re-imports pandas",
    )


def _wait_for_kernelspecs(port: int, deadline: float) -> None:
    """Poll the endpoint execd polls, through a deliberately proxy-less opener.

    urllib honours `HTTPS_PROXY`/`HTTP_PROXY` from the environment, and a
    loopback request routed through a proxy never arrives; inside the sandbox
    there is no proxy to trip over.
    """
    url = f"http://127.0.0.1:{port}/api/kernelspecs"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.perf_counter() < deadline:
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == HTTP_OK and json.loads(response.read())["kernelspecs"]:
                    return
        except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
            time.sleep(0.02)
    message = "jupyter server never served a kernelspec"
    raise RuntimeError(message)


def bench_jupyter(python: str, script: Path, csv_path: Path, repeats: int) -> Variant:
    from jupyter_client.manager import start_new_kernel  # noqa: PLC0415 - optional dep

    environment = _environment(csv_path)
    source = script.read_text(encoding="utf-8")

    # Exactly the CMD the sandbox image used to run, plus `--allow-root`:
    # jupyter refuses to start as uid 0 and this harness runs as root, while
    # the sandbox image runs as uid 10001 and needs no such flag. The flag
    # changes a startup permission check, not the boot work being measured.
    server = subprocess.Popen(  # noqa: S603
        [
            python, "-m", "jupyter", "server",
            "--ip=127.0.0.1", f"--port={JUPYTER_PORT}", "--no-browser",
            "--allow-root",
            "--IdentityProvider.token=", "--ServerApp.token=",
            "--ServerApp.password=", "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
    )
    try:
        boot_started = time.perf_counter()
        _wait_for_kernelspecs(JUPYTER_PORT, boot_started + BOOT_TIMEOUT_S)
        server_boot = time.perf_counter() - boot_started

        kernel_started = time.perf_counter()
        manager, client = start_new_kernel(kernel_name="python3", env=environment)
        kernel_boot = time.perf_counter() - kernel_started
        try:
            first_started = time.perf_counter()
            reply = client.execute_interactive(source, timeout=600, store_history=False)
            first = time.perf_counter() - first_started
            if reply["content"]["status"] != "ok":
                message = f"kernel execution failed: {reply['content']}"
                raise RuntimeError(message)

            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                client.execute_interactive(source, timeout=600, store_history=False)
                samples.append(time.perf_counter() - started)
            # Sampled while both processes are live and idle: this is the memory
            # a sandbox gives up to the Python substrate before running anything.
            rss = _tree_rss_mb(server.pid) + _kernel_rss_mb(manager)
        finally:
            client.stop_channels()
            manager.shutdown_kernel(now=True)
    finally:
        server.terminate()
        with suppress(subprocess.TimeoutExpired):
            server.wait(timeout=20)

    boot = server_boot + kernel_boot
    return Variant(
        name="jupyter",
        cold_start_seconds=boot + first,
        boot_seconds=boot,
        first_execution_seconds=first,
        warm_seconds=statistics.median(samples),
        warm_samples=samples,
        # The kernel keeps one namespace, so the body cannot self-report per run
        # the way a fresh process does; it is the same source over the same file.
        body_seconds=float("nan"),
        substrate_rss_mb=round(rss, 1),
        note=(
            f"server boot {server_boot:.2f}s + kernel spawn {kernel_boot:.2f}s; "
            "shared namespace across runs"
        ),
    )


def bench_forkrun(python: str, script: Path, csv_path: Path, repeats: int) -> Variant:
    environment = _environment(csv_path)
    with suppress(FileNotFoundError):
        FORKRUN_SOCKET.unlink()

    # The first call spawns the daemon and waits for it, so it carries the boot.
    first, stdout = _run([python, str(FORKRUN), str(script)], environment)
    samples = [
        _run([python, str(FORKRUN), str(script)], environment)[0] for _ in range(repeats)
    ]
    warm = statistics.median(samples)
    return Variant(
        name="forkrun",
        cold_start_seconds=first,
        # The daemon spawn is not separately observable from the client side;
        # it is whatever the first call paid above the steady-state cost.
        boot_seconds=max(0.0, first - warm),
        first_execution_seconds=warm,
        warm_seconds=warm,
        warm_samples=samples,
        body_seconds=_body_seconds(stdout),
        note="daemon pre-imports pandas/numpy; each run is a forked pristine child",
    )


def bench_coderun(python: str, script: Path, csv_path: Path, repeats: int) -> Variant:
    """Measure the path `POST /v1/executions` actually takes today.

    forkrun for the warm imports, coderun for the final-expression echo. This is
    the number to compare against `jupyter`, since `forkrun` alone skips the
    runner the endpoint really invokes.
    """
    environment = _environment(csv_path)
    environment["HARBORBOX_CODE_PATH"] = str(script)
    environment["HARBORBOX_RESULT_SENTINEL"] = "__harborbox_bench_sentinel__"
    with suppress(FileNotFoundError):
        FORKRUN_SOCKET.unlink()

    command = [python, str(FORKRUN), str(CODERUN)]
    first, stdout = _run(command, environment)
    samples = [_run(command, environment)[0] for _ in range(repeats)]
    warm = statistics.median(samples)
    return Variant(
        name="coderun",
        cold_start_seconds=first,
        boot_seconds=max(0.0, first - warm),
        first_execution_seconds=warm,
        warm_seconds=warm,
        warm_samples=samples,
        body_seconds=_body_seconds(stdout),
        note="forkrun daemon + coderun runner; what execute_code runs today",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument(
        "--script",
        type=Path,
        default=BENCH_DIR / "analysis.py",
        help="workload to run; noop.py isolates path overhead from body cost",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--skip-jupyter",
        action="store_true",
        help="skip the jupyter variant when jupyter-server is not installed",
    )
    arguments = parser.parse_args()

    if not arguments.csv.exists():
        message = f"missing benchmark CSV: {arguments.csv}"
        raise SystemExit(message)

    script = arguments.script
    variants = [bench_cold(arguments.python, script, arguments.csv, arguments.repeats)]
    if not arguments.skip_jupyter:
        variants.append(
            bench_jupyter(arguments.python, script, arguments.csv, arguments.repeats)
        )
    variants.append(bench_forkrun(arguments.python, script, arguments.csv, arguments.repeats))
    variants.append(bench_coderun(arguments.python, script, arguments.csv, arguments.repeats))

    report = {
        "csv_bytes": arguments.csv.stat().st_size,
        "script": script.name,
        "repeats": arguments.repeats,
        "python": arguments.python,
        "variants": [asdict(variant) for variant in variants],
    }
    rendered = json.dumps(report, indent=2)
    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)  # noqa: T201 - CLI output

    width = max(len(variant.name) for variant in variants)
    header = "\nvariant".ljust(width + 4)
    print(header, "cold_start", " boot", " warm", "substrate_rss", sep="  ")  # noqa: T201
    for variant in variants:
        print(  # noqa: T201
            variant.name.ljust(width + 4),
            f"{variant.cold_start_seconds:9.2f}s",
            f"{variant.boot_seconds:5.2f}s",
            f"{variant.warm_seconds:5.2f}s",
            f"{variant.substrate_rss_mb:12.1f} MB",
            sep="  ",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
