# Strict lint, full unit coverage, and a CI results report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give harborbox a CI pipeline that blocks on a strict ruff rule set with zero findings and 100% unit-test line coverage, and that posts a sticky per-package tests-and-coverage comment on every pull request.

**Architecture:** Three GitHub Actions jobs on the self-hosted `onvo-ci` runner — `test` (ruff, mypy, pytest with coverage), `e2e` (docker compose stack), and `report` (renders artifacts into a sticky PR comment). Gates are introduced non-blocking so the comment starts reporting honest numbers on day one, then flipped to blocking in the final task once the lint and coverage backlogs are cleared. `scripts/ci_report.py` reads only already-written artifacts and runs nothing, so it can never fail a build.

**Tech Stack:** Python 3.12, uv, ruff, mypy (strict), pytest + pytest-asyncio + pytest-cov + pytest-json-report, SQLAlchemy async, FastAPI, Docker SDK, GitHub Actions.

## Global Constraints

- Python 3.12. `requires-python = ">=3.12"`, ruff `target-version = "py312"`, mypy `python_version = "3.12"` — all three must agree, and a `.python-version` file pins it.
- All source files start with `from __future__ import annotations` where they already do; do not remove it.
- Tests use hand-written fakes, not `unittest.mock`. The house pattern is `tests/test_warm_pool.py` (`FakePool`, `FakeWarmHandle`) — plain classes with typed methods.
- `mypy` runs strict and currently passes on 25 files. It must still pass after every task.
- Every entry in the ruff `ignore` list and the coverage `exclude_lines` list carries a trailing comment giving the reason. An exemption without a reason is a plan failure.
- Never add a `# noqa` to silence a finding that should be fixed. If a `# noqa` is genuinely correct, it carries a reason comment.
- Never add `# pragma: no cover` to reach the coverage number. The only exclusions are the three documented in `[tool.coverage.report]`.
- Commit after every task. Commit messages are imperative mood, no `Co-Authored-By` trailer unless the repo already uses one.

## Baseline (measured 2026-08-14, commit 6e8d024)

| Metric | Value |
|---|---|
| `ruff check .` (current 6 families) | passes |
| `ruff check --select ALL` | 1524 findings |
| Findings after the documented ignore list | **444** (345 src/sandbox/scripts, 99 tests) |
| `mypy` | passes, 25 files |
| `pytest` | 115 tests passing |
| Line coverage over `src/` | **49%** — 1643 of 3239 statements uncovered |

Per-module uncovered statements, which are the burn-down targets in Tasks 12–19:

| Module | Uncovered | Cover |
|---|---:|---:|
| `harborbox/api.py` | 267 | 43% |
| `harborbox/scheduler.py` | 253 | 21% |
| `harborbox/opensandbox_runtime.py` | 185 | 46% |
| `harborbox/runtime.py` | 174 | 22% |
| `harborbox_agent/main.py` | 168 | 0% |
| `harborbox/postgres_pool_store.py` | 160 | 21% |
| `harborbox_agent/kernel.py` | 98 | 0% |
| `harborbox/opensandbox_compat.py` | 67 | 51% |
| `harborbox/template_builder.py` | 61 | 51% |
| `harborbox_sdk/models.py` | 50 | 64% |
| `harborbox/warm_pool.py` | 43 | 65% |
| `harborbox/reaper.py` | 41 | 45% |
| `harborbox_sdk/client.py` | 15 | 52% |
| `harborbox_agent/output.py` | 14 | 0% |
| `harborbox/presenters.py` | 9 | 50% |
| `harborbox/config.py` | 8 | 95% |
| `harborbox/db.py` | 5 | 71% |
| `harborbox/execution_secrets.py` | 4 | 89% |
| `harborbox/security.py` | 4 | 50% |
| `harborbox/runtime_factory.py` | 4 | 50% |
| `harborbox/schemas.py` | 3 | 98% |
| `harborbox/templates.py` | 2 | 99% |
| `harborbox/main.py` | 2 | 0% |

## How to read the burn-down tasks

Tasks 6–9 (lint) and Tasks 12–19 (coverage) are burn-downs of a known, counted
backlog. This plan does **not** inline all 444 lint fixes or all ~1600 tests —
that would be tens of thousands of lines and would rot the moment the first fix
shifts a line number.

For those tasks the contract is instead:

1. **The enumerated scope** — the exact rule codes or the exact module, listed in the task.
2. **A worked example** — real code, from this repo, showing the pattern to apply.
3. **An objective completion command** — a `ruff`/`pytest` invocation that exits zero only when the task is genuinely finished.

A burn-down task is done when its completion command exits zero. Not before.
Do not move to the next task with a red completion command.

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `.python-version` | Pins 3.12 so uv, ruff and mypy agree locally and in CI. |
| `scripts/ci_report.py` | Renders CI artifacts into the Markdown report. Pure function of files on disk. |
| `tests/test_ci_report.py` | Unit tests for the report renderer, including missing and malformed inputs. |
| `tests/conftest.py` | Shared fixtures: the fake Docker client and the real-Postgres session factory. |
| `tests/fakes/__init__.py` | Package marker for the fakes. |
| `tests/fakes/docker.py` | `FakeDockerClient` and friends — the Docker SDK surface `runtime.py` uses. |
| `.github/workflows/ci.yml` | The three-job pipeline. |

**Modified:**

| Path | Change |
|---|---|
| `pyproject.toml` | Dev deps, coverage config, pytest markers, strict ruff config. |
| `tests/e2e_smoke.py`, `e2e_oom.py`, `e2e_large_upload.py`, `e2e_onvo_readiness.py` | Converted from `__main__` scripts to `@pytest.mark.e2e` tests. |
| `src/**/*.py` | Lint fixes (Tasks 6–9). No behaviour changes. |

**New test modules** (Tasks 12–19), one per module under test, following the
existing `tests/test_<module>.py` convention: `test_runtime.py`,
`test_postgres_pool_store.py`, `test_scheduler.py`, `test_api.py`,
`test_opensandbox_compat.py`, `test_agent_main.py`, `test_agent_kernel.py`,
`test_agent_output.py`, `test_sdk_client.py`, `test_presenters.py`,
`test_db.py`, `test_security.py`, `test_runtime_factory.py`, `test_config.py`.

---

### Task 1: Pin Python and add coverage tooling

**Files:**
- Create: `.python-version`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: `coverage.json` and `unit-results.json` artifacts consumed by Task 3; the `e2e` pytest marker consumed by Task 2.

- [ ] **Step 1: Pin the interpreter**

There is no `.python-version`, so `uv` resolved to 3.14.4 on the machine this
plan was written on while every tool config says 3.12. Create `.python-version`:

```
3.12
```

- [ ] **Step 2: Add the dev dependencies**

In `pyproject.toml`, extend `[project.optional-dependencies].dev`:

```toml
dev = [
  "mypy>=1.17.0",
  "pytest>=8.4.0",
  "pytest-asyncio>=1.1.0",
  "pytest-cov>=6.0.0",
  "pytest-json-report>=1.5.0",
  "ruff>=0.12.0",
]
```

- [ ] **Step 3: Register the e2e marker and keep e2e out of the default run**

Replace the `[tool.pytest.ini_options]` block:

```toml
[tool.pytest.ini_options]
# -m "not e2e" keeps the live-stack suites out of the default and CI unit runs.
# The e2e job opts back in with `pytest -m e2e`.
addopts = "-q -m 'not e2e'"
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
  "e2e: requires a live Compose stack (postgres, opensandbox, harborbox-api)",
  "postgres: requires a real Postgres; SQLite cannot run the pg-dialect SQL under test",
]
```

- [ ] **Step 4: Add the coverage config**

Append to `pyproject.toml`. `fail_under` is deliberately absent here — Task 20
adds it, once the backlog is actually clear.

```toml
[tool.coverage.run]
source = ["src"]
branch = false

[tool.coverage.report]
show_missing = true
exclude_lines = [
  "pragma: no cover",
  "if TYPE_CHECKING:",              # import-time-only; never executes at runtime
  "raise NotImplementedError",      # runtime_protocol.py stubs, by definition unreachable
  "if __name__ == \"__main__\":",   # uvicorn and agent entrypoints, not unit-testable
]
```

- [ ] **Step 5: Verify the toolchain resolves to 3.12 and coverage renders**

Run:

```bash
uv sync --extra dev && uv run python -V && uv run pytest --cov --cov-report=json --json-report --json-report-file=unit-results.json
```

Expected: `Python 3.12.x`; 115 tests pass; `coverage.json` and
`unit-results.json` both exist. Confirm the total is still 49% — this task
changes no coverage, it only makes it machine-readable.

- [ ] **Step 6: Verify mypy and ruff still pass on 3.12**

Run: `uv run mypy && uv run ruff check .`
Expected: both exit zero. If mypy now reports errors it did not report on
3.14, fix them here — that is the version pin doing its job.

- [ ] **Step 7: Commit**

```bash
git add .python-version pyproject.toml
git commit -m "Pin Python 3.12 and make test results machine-readable"
```

---

### Task 2: Convert the E2E scripts to pytest

**Files:**
- Create: `tests/conftest.py`
- Modify: `tests/e2e_smoke.py`, `tests/e2e_oom.py`, `tests/e2e_large_upload.py`, `tests/e2e_onvo_readiness.py`

**Interfaces:**
- Consumes: the `e2e` marker from Task 1.
- Produces: `tests/conftest.py` holding the shared `client` fixture, which Task 11 extends with the Docker and Postgres fixtures; `pytest -m e2e --json-report` emitting counts that Task 3 reads.

The four files are standalone `__main__` scripts driven by `HARBORBOX_API_KEY`.
They produce no machine-readable result, so the report cannot count them.

- [ ] **Step 1: Create `tests/conftest.py` with the shared client fixture**

All four e2e modules need the same live-stack client. It goes in `conftest.py`
from the start rather than being duplicated per file. Task 11 extends this same
file with the Docker and Postgres fixtures.

```python
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from harborbox_sdk import SandboxClient


@pytest.fixture
def client() -> Iterator[SandboxClient]:
    """A live-stack SDK client. Only meaningful for tests marked `e2e`."""
    api_key = os.environ.get("HARBORBOX_API_KEY")
    if not api_key:
        pytest.fail("HARBORBOX_API_KEY is required for e2e tests")
    with SandboxClient(api_key=api_key) as live:
        yield live
```

- [ ] **Step 2: Convert `tests/e2e_smoke.py`**

Its current shape is a `def main() -> None:` reading `os.environ["HARBORBOX_API_KEY"]`
with a trailing `if __name__ == "__main__": main()`. Convert to:

```python
"""Runs against a live local Compose stack. Selected with `pytest -m e2e`."""

from __future__ import annotations

import time

import pytest

from harborbox_sdk import SandboxClient


@pytest.mark.e2e
def test_two_sandboxes_run_code_in_parallel(client: SandboxClient) -> None:
    first = client.sandboxes.create(memory_mb=128, cpu=1, idle_timeout_seconds=60)
    second = client.sandboxes.create(memory_mb=128, cpu=1, idle_timeout_seconds=60)
    try:
        started = time.monotonic()
        first_job = first.run_code(
            "import time; time.sleep(2); first_value = 40; first_value + 2", wait=False
        )
        second_job = second.run_code(
            "import time; time.sleep(2); second_value = 5; second_value * 2", wait=False
        )
        first_job.wait(timeout=60, raise_on_error=True)
        second_job.wait(timeout=60, raise_on_error=True)
        elapsed = time.monotonic() - started
        assert first_job.text == "42", first_job.error
        assert second_job.text == "10", second_job.error
        assert first_job.started_at and first_job.finished_at
        assert elapsed < 10, f"parallel run took {elapsed:.1f}s; sandboxes serialised"
    finally:
        first.kill()
        second.kill()
```

Use `pytest.fail` rather than `KeyError` for the missing key so a
misconfigured runner reports a legible reason. Preserve every existing
assertion and its message; do not weaken any of them. Keep the cleanup in a
`finally` so a failing assertion still tears the sandboxes down.

- [ ] **Step 3: Convert the other three the same way**

`e2e_oom.py`, `e2e_large_upload.py`, `e2e_onvo_readiness.py`. Same shape:
module docstring, the `client` fixture taken from `conftest.py` (do not
redefine it per file), `@pytest.mark.e2e` on each test, `__main__` block
deleted, every original assertion preserved.

Read each file before converting. Some have multiple logical phases in one
`main()` — split those into separate `test_` functions, one per phase, so the
report counts them individually and a failure names the phase.

- [ ] **Step 4: Verify e2e is excluded from the default run**

Run: `uv run pytest --collect-only -q | tail -3`
Expected: the count is still 115. The e2e tests are collected but deselected
by `-m 'not e2e'`.

- [ ] **Step 5: Verify e2e is selectable and reports counts**

Run: `uv run pytest -m e2e --collect-only -q | tail -3`
Expected: lists the converted e2e tests (at least 4). This only collects — it
does not run them, because there is no live stack locally.

- [ ] **Step 6: Verify lint and types still pass**

Run: `uv run mypy && uv run ruff check .`
Expected: both exit zero.

- [ ] **Step 7: Commit**

```bash
git add tests/
git commit -m "Convert the e2e scripts to marked pytest tests"
```

---

### Task 3: The report renderer

**Files:**
- Create: `scripts/ci_report.py`, `tests/test_ci_report.py`

**Interfaces:**
- Consumes: `coverage.json`, `unit-results.json`, `ruff-strict.json`, `e2e-results.json` — all optional.
- Produces: `render_report(root: Path) -> str`, and a `__main__` block writing it to stdout. Task 4's workflow calls `python scripts/ci_report.py > ci-report.md`.

This is `src`-adjacent code parsing four JSON shapes, and its failure mode is
quiet — a wrong number in a comment everyone trusts. It gets real TDD.

Port onvo's `scripts/ci-report.mjs` contract: read only artifacts already
written, run nothing, and render a package with no data as `—` rather than
dropping the row, because a silently missing row reads as "fine" when it
usually means the step crashed.

- [ ] **Step 1: Write the failing test for the happy path**

Create `tests/test_ci_report.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from ci_report import render_report


def write(root: Path, name: str, payload: object) -> None:
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def test_renders_a_row_per_package_with_coverage_and_counts(tmp_path: Path) -> None:
    write(
        tmp_path,
        "coverage.json",
        {
            "files": {
                "src/harborbox/api.py": {"summary": {"covered_lines": 8, "num_statements": 10}},
                "src/harborbox_sdk/client.py": {"summary": {"covered_lines": 3, "num_statements": 3}},
            }
        },
    )
    write(tmp_path, "unit-results.json", {"summary": {"total": 12, "passed": 12, "failed": 0}})

    report = render_report(tmp_path)

    assert "| `harborbox` | 12 | 80.0% |" in report
    assert "`harborbox_sdk`" in report
    assert "100.0%" in report
    assert "Total unit test coverage: 84.6%" in report
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_ci_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ci_report'`.

- [ ] **Step 3: Make `scripts/` importable from tests**

Add to `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
pythonpath = ["scripts"]
```

- [ ] **Step 4: Write `scripts/ci_report.py`**

```python
"""Render CI artifacts into the Markdown block posted to the job summary and PR.

Reads only files the earlier steps already wrote, runs nothing itself, and so
cannot fail a build. Every input is optional; a package with no data renders as
"—" rather than being dropped, because a silently missing row reads as "fine"
when it usually means the step crashed.

Usage:  python scripts/ci_report.py > ci-report.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PACKAGES = ("harborbox", "harborbox_agent", "harborbox_sdk")

MARKER = "<!-- harborbox-ci-report -->"

FOOTNOTE = (
    "<sub>Unit coverage is line coverage from pytest-cov across each package's "
    "whole tree in `src`. E2E tests run against a local Compose stack built from "
    "this commit. The lint backlog is what `ruff --select ALL` reports with "
    "nothing ignored; the blocking `ruff check` gate is green at zero. Coverage "
    "is enforced at 100% by the blocking pytest job.</sub>"
)


def read_json(path: Path) -> Any:
    """Return parsed JSON, or None if absent or malformed."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def package_of(filename: str) -> str | None:
    """Map "src/harborbox/api.py" to "harborbox"."""
    parts = Path(filename).parts
    for index, part in enumerate(parts):
        if part == "src" and index + 1 < len(parts):
            candidate = parts[index + 1]
            return candidate if candidate in PACKAGES else None
    return None


def coverage_by_package(coverage: Any) -> dict[str, tuple[int, int]]:
    """Return {package: (covered_lines, total_statements)}."""
    totals: dict[str, tuple[int, int]] = {}
    files = (coverage or {}).get("files", {})
    for filename, entry in files.items():
        package = package_of(filename)
        if package is None:
            continue
        summary = entry.get("summary", {})
        covered, total = totals.get(package, (0, 0))
        totals[package] = (
            covered + int(summary.get("covered_lines", 0)),
            total + int(summary.get("num_statements", 0)),
        )
    return totals


def pct(covered: int, total: int) -> str:
    return f"{(covered / total * 100):.1f}%" if total else "—"


def counts(results: Any) -> tuple[int, int] | None:
    """Return (total, failed) from a pytest-json-report summary."""
    summary = (results or {}).get("summary")
    if not isinstance(summary, dict):
        return None
    return int(summary.get("total", 0)), int(summary.get("failed", 0))


def cell(pair: tuple[int, int] | None) -> str:
    if pair is None:
        return "—"
    total, failed = pair
    return f"{failed}/{total} failed" if failed else str(total)


def lint_by_rule(findings: Any) -> dict[str, int]:
    by_rule: dict[str, int] = {}
    for item in findings if isinstance(findings, list) else []:
        rule = (item.get("code") or "(no rule)") if isinstance(item, dict) else "(no rule)"
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return by_rule


def render_report(root: Path) -> str:
    coverage = coverage_by_package(read_json(root / "coverage.json"))
    unit = counts(read_json(root / "unit-results.json"))
    e2e = counts(read_json(root / "e2e-results.json"))
    by_rule = lint_by_rule(read_json(root / "ruff-strict.json"))
    backlog = sum(by_rule.values())

    lines = [
        "### Tests and coverage",
        "",
        "| Package | Unit tests | Unit test coverage | E2E tests |",
        "|---|---:|---:|---:|",
    ]
    # Unit counts are repo-wide, not per package: pytest reports one total and
    # attributing it per package would mean parsing test paths and guessing.
    # The number is shown on the first row and the rest carry "—".
    for index, package in enumerate(PACKAGES):
        covered, total = coverage.get(package, (0, 0))
        unit_cell = cell(unit) if index == 0 else "—"
        e2e_cell = cell(e2e) if index == 0 else "—"
        lines.append(
            f"| `{package}` | {unit_cell} | {pct(covered, total)} | {e2e_cell} |"
        )

    covered_all = sum(c for c, _ in coverage.values())
    total_all = sum(t for _, t in coverage.values())
    lines += ["", f"**{_summary(unit, 'unit test')} {_summary(e2e, 'E2E test')}**"]
    lines.append(f"**Total unit test coverage: {pct(covered_all, total_all)}.**")

    if backlog:
        top = sorted(by_rule.items(), key=lambda kv: -kv[1])[:8]
        lines += [
            "",
            f"<details><summary><b>Lint backlog: {backlog}</b> — what "
            "<code>ruff --select ALL</code> reports with nothing ignored "
            "(click for the top rules)</summary>",
            "",
            "| Rule | Count |",
            "|---|---:|",
            *(f"| `{rule}` | {n} |" for rule, n in top),
            "",
            "These are the families `pyproject.toml` deliberately ignores, each "
            "with its reason. The blocking gate is green at zero.",
            "</details>",
        ]

    lines += ["", FOOTNOTE]
    return "\n".join(lines) + "\n"


def _summary(pair: tuple[int, int] | None, noun: str) -> str:
    if pair is None:
        return f"{noun.capitalize()} results unavailable."
    total, failed = pair
    if failed:
        return f"{failed} failing {noun}{'' if failed == 1 else 's'} out of {total}."
    return f"{total} {noun}{'' if total == 1 else 's'} passing."


if __name__ == "__main__":
    sys.stdout.write(render_report(Path.cwd()))
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `uv run pytest tests/test_ci_report.py -v`
Expected: PASS.

- [ ] **Step 6: Write the failing tests for the degraded paths**

These are the reason this module has tests at all. Append to `tests/test_ci_report.py`:

```python
def test_missing_artifacts_render_as_dashes_not_zeros(tmp_path: Path) -> None:
    report = render_report(tmp_path)

    assert "| `harborbox` | — | — | — |" in report
    assert "Unit test results unavailable." in report
    assert "Total unit test coverage: —." in report


def test_malformed_json_is_treated_as_absent(tmp_path: Path) -> None:
    (tmp_path / "coverage.json").write_text("{not json", encoding="utf-8")

    report = render_report(tmp_path)

    assert "— |" in report


def test_failures_are_reported_rather_than_hidden(tmp_path: Path) -> None:
    write(tmp_path, "unit-results.json", {"summary": {"total": 10, "failed": 3}})

    report = render_report(tmp_path)

    assert "3/10 failed" in report
    assert "3 failing unit tests out of 10." in report


def test_backlog_section_is_omitted_when_the_backlog_is_clear(tmp_path: Path) -> None:
    write(tmp_path, "ruff-strict.json", [])

    assert "Lint backlog" not in render_report(tmp_path)


def test_backlog_lists_the_top_rules_by_count(tmp_path: Path) -> None:
    write(
        tmp_path,
        "ruff-strict.json",
        [{"code": "D103"}] * 3 + [{"code": "CPY001"}],
    )

    report = render_report(tmp_path)

    assert "<b>Lint backlog: 4</b>" in report
    assert "| `D103` | 3 |" in report


def test_files_outside_the_known_packages_are_ignored(tmp_path: Path) -> None:
    write(
        tmp_path,
        "coverage.json",
        {"files": {"scripts/ci_report.py": {"summary": {"covered_lines": 1, "num_statements": 4}}}},
    )

    assert "Total unit test coverage: —." in render_report(tmp_path)
```

- [ ] **Step 7: Run them and fix what fails**

Run: `uv run pytest tests/test_ci_report.py -v`
Expected: all pass. If `test_missing_artifacts_render_as_dashes_not_zeros`
fails because `pct(0, 0)` returned `0.0%`, that is the bug those tests exist to
catch — a package whose coverage step crashed must not read as 0% coverage,
which looks like a real measurement.

- [ ] **Step 8: Verify lint and types**

Run: `uv run mypy && uv run ruff check .`
Expected: both exit zero. `scripts/` is outside mypy's `packages`, so if
`ci_report.py` is unchecked, add it — this file deserves the strict treatment.

- [ ] **Step 9: Commit**

```bash
git add scripts/ci_report.py tests/test_ci_report.py pyproject.toml
git commit -m "Render CI artifacts into a Markdown results report"
```

---

### Task 4: The CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `scripts/ci_report.py` from Task 3; the `e2e` marker from Task 2.
- Produces: the `coverage`, `lint-reports` and `e2e-results` artifacts, and the sticky PR comment.

Gates are `continue-on-error: true` in this task and flipped in Task 20. That
is deliberate: it gets the report comment working and reporting honest numbers
while the backlog is still being burned down, instead of leaving CI red for the
whole branch and training everyone to ignore it.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    name: Unit tests
    # Self-hosted for the reason onvo's ci.yml documents: the org's Actions
    # spend is blocked, so every GitHub-hosted job fails in ~2s with "recent
    # account payments have failed". Self-hosted runners are not billed.
    runs-on: [self-hosted, onvo-ci]
    permissions:
      contents: read

    # postgres_pool_store.py uses dialects.postgresql.insert and
    # with_for_update(skip_locked=True). SQLite cannot execute either, so its
    # tests need the real thing rather than a fake session.
    services:
      postgres:
        image: postgres:17-alpine
        env:
          POSTGRES_USER: harborbox
          POSTGRES_PASSWORD: harborbox
          POSTGRES_DB: harborbox_test
        options: >-
          --health-cmd "pg_isready -U harborbox"
          --health-interval 5s --health-timeout 5s --health-retries 20
        ports: ["5432:5432"]

    env:
      HARBORBOX_TEST_DATABASE_URL: postgresql+asyncpg://harborbox:harborbox@127.0.0.1:5432/harborbox_test

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --extra dev

      # Blocking from Task 20. Until the 444-finding backlog is cleared this
      # reports without failing the build.
      - name: Lint
        continue-on-error: true
        run: uv run ruff check .

      - name: Typecheck
        run: uv run mypy

      # Blocking from Task 20, when fail_under = 100 is added.
      - name: Tests and coverage
        continue-on-error: true
        run: >-
          uv run pytest --cov --cov-report=json --cov-report=term-missing
          --json-report --json-report-file=unit-results.json

      # The backlog behind the ignore list, printed on every run rather than
      # discovered by someone going looking. Expected to be non-zero: it is the
      # docstring, copyright and formatter-conflict families, by design.
      - name: Lint backlog (non-blocking)
        if: always()
        run: uv run ruff check --select ALL --output-format=json -o ruff-strict.json . || true

      - name: Upload reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: unit-reports
          path: |
            coverage.json
            unit-results.json
            ruff-strict.json
          if-no-files-found: warn
          retention-days: 14

  e2e:
    name: E2E (Compose)
    runs-on: [self-hosted, onvo-ci]
    timeout-minutes: 40
    permissions:
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --extra dev

      - name: Build the stack
        run: docker compose build

      - name: Start the stack
        run: docker compose up -d --wait

      - name: Run the e2e suites
        env:
          HARBORBOX_API_KEY: ${{ secrets.HARBORBOX_E2E_API_KEY }}
        run: uv run pytest -m e2e --json-report --json-report-file=e2e-results.json

      - name: Stack logs on failure
        if: failure()
        run: docker compose logs --no-color --tail 300

      - name: Tear down
        if: always()
        run: docker compose down -v

      - name: Upload e2e results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: e2e-reports
          path: e2e-results.json
          if-no-files-found: warn
          retention-days: 14

  report:
    name: Results report
    if: always()
    needs: [test, e2e]
    runs-on: [self-hosted, onvo-ci]
    # pull-requests: write is only for the sticky comment. The default token is
    # read-only here, so without it the comment step 403s while every other step
    # passes — which reads as "the comment is broken" rather than "missing scope".
    permissions:
      contents: read
      pull-requests: write

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Download unit reports
        continue-on-error: true
        uses: actions/download-artifact@v4
        with:
          name: unit-reports
          path: .

      - name: Download e2e reports
        continue-on-error: true
        uses: actions/download-artifact@v4
        with:
          name: e2e-reports
          path: .

      - name: Build the report
        run: |
          uv run python scripts/ci_report.py > ci-report.md
          cat ci-report.md >> "$GITHUB_STEP_SUMMARY"

      - name: Comment on the PR
        if: github.event_name == 'pull_request'
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const marker = '<!-- harborbox-ci-report -->';
            const body = marker + '\n' + fs.readFileSync('ci-report.md', 'utf8');
            const { owner, repo } = context.repo;
            const issue_number = context.payload.pull_request.number;

            // Update in place rather than appending a comment per push, so a
            // long-lived PR does not accumulate a dozen stale tables. The HTML
            // marker is how we find our own comment again.
            const existing = await github.paginate(
              github.rest.issues.listComments,
              { owner, repo, issue_number, per_page: 100 },
            );
            const mine = existing.find((c) => c.body?.startsWith(marker));

            if (mine) {
              await github.rest.issues.updateComment({ owner, repo, comment_id: mine.id, body });
            } else {
              await github.rest.issues.createComment({ owner, repo, issue_number, body });
            }
```

- [ ] **Step 2: Validate the YAML parses**

Run: `uv run python -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text())" && echo OK`
Expected: `OK`. If PyYAML is absent, `uv run --with pyyaml python -c ...`.

- [ ] **Step 3: Verify the report renders from a real local run**

Run:

```bash
uv run pytest --cov --cov-report=json --json-report --json-report-file=unit-results.json; uv run ruff check --select ALL --output-format=json -o ruff-strict.json . || true; uv run python scripts/ci_report.py
```

Expected: a Markdown table with three package rows, roughly 49% total coverage,
and a `Lint backlog: 1524` details block. Those are today's honest numbers.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Add the CI pipeline with a sticky PR results comment"
```

- [ ] **Step 5: Push and confirm the runner picks the job up**

```bash
git push -u origin claude/lint-strict-unit-tests-91d672
```

**This is the checkpoint for the biggest open risk in the plan.** Listing org
runners returns 403 with the available token, so whether `onvo-ci` is org-level
or scoped to the onvo repo is unconfirmed. If the jobs sit in "Queued" for more
than a few minutes, the label is not available to this repo — stop and report
that, rather than waiting. The fix is either granting harborbox access to the
runner group or changing the label; both are the user's call.

---

### Task 5: Turn on the strict ruff configuration

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: a 444-finding backlog that Tasks 6–9 burn down.

- [ ] **Step 1: Replace the lint configuration**

In `pyproject.toml`, replace the whole `[tool.ruff.lint]` block:

```toml
[tool.ruff.lint]
# Inverted default: everything is on, and each exemption below is a decision on
# record rather than an unexamined omission. New ruff rules arrive enabled.
select = ["ALL"]
ignore = [
  "D100",     # Docstring-on-every-module is not this repo's convention.
  "D101",     # ...nor on every class.
  "D102",     # ...nor on every method.
  "D103",     # ...nor on every function.
  "D104",     # ...nor on every package.
  "D105",     # ...nor on every magic method.
  "D107",     # ...nor on every __init__.
  "D203",     # Incompatible with D211, which ruff prefers.
  "D213",     # Incompatible with D212, which ruff prefers.
  "CPY001",   # This repo carries no copyright headers.
  "COM812",   # Conflicts with the formatter's trailing-comma handling.
  "B008",     # FastAPI's Depends/Query call defaults are framework conventions.
  "FAST002",  # Same: FastAPI's non-annotated dependency style is intentional.
  "ASYNC240", # The sandbox agent intentionally performs tiny local filesystem calls.
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = [
  "S101",     # assert is pytest's idiom.
  "INP001",   # tests/ is intentionally not an importable package.
  "SLF001",   # Tests reach into private state to set up fixtures; see test_warm_pool.py.
]
"scripts/**" = ["INP001"]  # scripts/ is intentionally not an importable package.
```

- [ ] **Step 2: Measure the backlog this creates**

Run: `uv run ruff check . --statistics`
Expected: roughly 444 findings (345 in src/sandbox/scripts, 99 in tests), led by
`TRY003` 75, `PLR2004` 40, `SLF001` 40, `EM102` 38, `EM101` 37, `ANN401` 27,
`TC001` 22. Record the exact number in the commit message — it is the burn-down
target and Tasks 6–9 must reduce it to zero.

- [ ] **Step 3: Apply the safe automatic fixes**

Run: `uv run ruff check --fix . && uv run ruff check . --statistics`
Expected: 13 findings fixed. Do **not** use `--unsafe-fixes`; those 136 need
review and are handled per-family in Tasks 6–9.

- [ ] **Step 4: Verify nothing broke**

Run: `uv run pytest && uv run mypy`
Expected: 115 tests pass, mypy exits zero.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/ tests/ scripts/ sandbox/
git commit -m "Select the full ruff rule set with a documented ignore list"
```

---

### Task 6: Clear the exception-style findings (~157)

**Files:**
- Modify: `src/harborbox/**`, `src/harborbox_agent/**`, `src/harborbox_sdk/**`

**Interfaces:**
- Consumes: the strict config from Task 5.
- Produces: nothing later tasks import.

**Scope — these rule codes only:** `TRY003` (75), `EM101` (37), `EM102` (38),
`TRY300` (6), `TRY301` (1).

- [ ] **Step 1: See the current state**

Run: `uv run ruff check --select TRY003,EM101,EM102,TRY300,TRY301 . --statistics`

- [ ] **Step 2: Apply the EM101/EM102 pattern**

Both rules want the message bound to a name before the `raise`, so tracebacks
are not cluttered by a long inline literal. `runtime.py` has many of these:

```python
# Before — EM102
raise SandboxUnavailable(f"container {name} is not running")

# After
message = f"container {name} is not running"
raise SandboxUnavailable(message)
```

- [ ] **Step 3: Apply the TRY003 pattern**

`TRY003` objects to long messages at the `raise` site of a *vanilla* exception.
The fix that is worth making is giving the exception class the message, not
mechanically hoisting strings. `runtime.py` already defines `RuntimeErrorBase`,
`SandboxUnavailable` and `SandboxMemoryExceeded`:

```python
# Before — TRY003
raise SandboxUnavailable(f"sandbox {sandbox.id} exceeded its memory limit")

# After
class SandboxMemoryExceeded(RuntimeErrorBase):
    def __init__(self, sandbox_id: str) -> None:
        super().__init__(f"sandbox {sandbox_id} exceeded its memory limit")

raise SandboxMemoryExceeded(sandbox.id)
```

Where a message is genuinely one-off and a dedicated class would be noise,
hoisting to a local as in Step 2 is the correct fix.

- [ ] **Step 4: Apply the TRY300 pattern**

```python
# Before — TRY300: the return belongs in `else`, so it cannot be reached by
# a path the except clause was meant to guard.
try:
    value = parse(raw)
    return value
except ValueError:
    return None

# After
try:
    value = parse(raw)
except ValueError:
    return None
else:
    return value
```

- [ ] **Step 5: Completion command**

Run: `uv run ruff check --select TRY003,EM101,EM102,TRY300,TRY301 .`
Expected: `All checks passed!`

- [ ] **Step 6: Verify behaviour is unchanged**

Run: `uv run pytest && uv run mypy`
Expected: 115 tests pass, mypy exits zero. These are message-shape changes; if
a test fails it is asserting on an exception string, and the assertion — not
the fix — should be updated.

- [ ] **Step 7: Commit**

```bash
git add src/
git commit -m "Give exceptions their messages rather than building them at the raise"
```

---

### Task 7: Clear the typing and annotation findings (~70)

**Files:**
- Modify: `src/harborbox/**`, `src/harborbox_agent/**`, `src/harborbox_sdk/**`

**Scope — these rule codes only:** `TC001` (22), `TC003` (12), `TC002` (7),
`ANN401` (27), `ANN003` (1), `ANN201` (1).

- [ ] **Step 1: Apply the TC001/TC002/TC003 pattern**

These move imports used only in annotations under `if TYPE_CHECKING:`, which
is safe because every module already has `from __future__ import annotations`.
`kernel.py` is the model already in the repo:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harborbox.models import Sandbox
```

Ruff can do most of these: `uv run ruff check --select TC001,TC002,TC003 --fix .`
Verify afterwards that nothing moved an import that is needed at runtime — a
Pydantic model field annotation *is* evaluated at runtime, so an import feeding
a Pydantic or SQLAlchemy declarative field must stay at module level. If ruff
moves one, revert that hunk and add a `# noqa: TC001` with the reason.

- [ ] **Step 2: Apply the ANN401 pattern**

`ANN401` flags `Any`. Replace with the real type where one exists:

```python
# Before
def _connect_egress(self, container: Any, sandbox: Sandbox) -> None:

# After
def _connect_egress(self, container: Container, sandbox: Sandbox) -> None:
```

with `from docker.models.containers import Container` under `TYPE_CHECKING`.
Where `Any` is genuinely correct — the Docker SDK's untyped `info()` dict, a
kernel message payload — keep it and add `# noqa: ANN401` with the reason.
mypy strict is the check that these are honest.

- [ ] **Step 3: Completion command**

Run: `uv run ruff check --select TC001,TC002,TC003,ANN401,ANN003,ANN201 .`
Expected: `All checks passed!`

- [ ] **Step 4: Verify**

Run: `uv run pytest && uv run mypy`
Expected: 115 tests pass, mypy exits zero. mypy matters more than usual here —
a wrongly-moved `TYPE_CHECKING` import shows up as a runtime `NameError`, and
these tests are what catch it.

- [ ] **Step 5: Commit**

```bash
git add src/
git commit -m "Move annotation-only imports behind TYPE_CHECKING and narrow Any"
```

---

### Task 8: Clear the security and correctness findings (~43)

**Files:**
- Modify: `src/**`, `sandbox/forkrun.py`, `tests/**`

**Scope — these rule codes only:** `S108` (14), `BLE001` (7), `N818` (6),
`SIM105` (6), `S110` (4), `S106` (2), `S105` (1), `S113` (1), `S603` (1),
`S607` (1).

These are the findings most likely to be real defects rather than style. Read
each one before changing it.

- [ ] **Step 1: Triage S108 (hardcoded temp file)**

14 findings, mostly `/tmp/...` paths inside the sandbox agent, where a fixed
path in a single-tenant container is intentional. For each: if the path is
inside the sandbox container, add `# noqa: S108` with the reason. If it is on
the host, replace with `tempfile.mkdtemp()`. Do not blanket-ignore the rule.

- [ ] **Step 2: Fix BLE001 (blind except) and S110 (try-except-pass)**

```python
# Before — BLE001 + S110: swallows KeyboardInterrupt and real bugs alike
try:
    container.kill()
except Exception:
    pass

# After
try:
    container.kill()
except (APIError, NotFound) as error:
    logger.debug("container %s already gone: %s", container.id, error)
```

Where the intent genuinely is "best effort, never raise", use
`contextlib.suppress` with the narrowest exception tuple that covers it — which
also clears the `SIM105` findings.

- [ ] **Step 3: Fix N818 (exception naming)**

6 findings. `N818` wants exception classes suffixed `Error`. `runtime.py` has
`SandboxUnavailable` and `SandboxMemoryExceeded`. Rename to
`SandboxUnavailableError` and `SandboxMemoryExceededError`, then update every
import and `except` clause. Search first:

```bash
grep -rn "SandboxUnavailable\|SandboxMemoryExceeded" src/ tests/
```

`RuntimeErrorBase` already ends in `Error` and needs no change.

- [ ] **Step 4: Triage S105/S106 (hardcoded passwords)**

3 findings. These are near-certainly test fixtures or default header names, not
real secrets. Confirm each by reading it. If it is a literal like
`x_sandbox_token`, it is a false positive — `# noqa: S106` with the reason. **If
any turns out to be a real credential, stop and report it rather than
annotating it.**

- [ ] **Step 5: Fix S113, S603, S607**

`S113` is an `httpx`/`requests` call without a timeout — add one, this is a real
hang risk. `S603`/`S607` are in `sandbox/forkrun.py` calling `subprocess` with a
partial path; use an absolute path, or `# noqa` with the reason if the binary is
resolved from the container's controlled `PATH`.

- [ ] **Step 6: Completion command**

Run: `uv run ruff check --select S108,S110,S105,S106,S113,S603,S607,BLE001,SIM105,N818 .`
Expected: `All checks passed!`

- [ ] **Step 7: Verify**

Run: `uv run pytest && uv run mypy`
Expected: 115 tests pass, mypy exits zero.

- [ ] **Step 8: Commit**

```bash
git add src/ sandbox/ tests/
git commit -m "Narrow blind excepts, name exceptions Error, and close the timeout gap"
```

---

### Task 9: Clear the remaining findings (~174)

**Files:**
- Modify: `src/**`, `tests/**`, `sandbox/**`, `scripts/**`

**Scope:** every rule still reported. At the time of writing that is `PLR2004`
(40), `SLF001` (40 — src only; tests are exempted per-file), `PLC0415` (18),
`SIM117` (16), `Q003` (8), `D401` (7), `T201` (5), `C901` (4), `FBT001` (4),
`FBT003` (4), `PT018` (4), `PLR0913` (3), `ARG001` (2), `D403` (2), `FURB162`
(2), `PTH123` (2), `PYI034` (2), plus fourteen single-finding rules.

- [ ] **Step 1: Apply the automatic fixes first**

Run: `uv run ruff check --fix . && uv run ruff check . --statistics`
This clears `Q003`, `D403`, `D202`, `C420`, `FURB167` and similar.

- [ ] **Step 2: Fix PLR2004 (magic values)**

Name the constant at module level:

```python
# Before
if response.status_code == 404:

# After
HTTP_NOT_FOUND = 404
...
if response.status_code == HTTP_NOT_FOUND:
```

Prefer `http.HTTPStatus.NOT_FOUND` from the standard library where the value is
an HTTP status.

- [ ] **Step 3: Fix SIM117 (nested with)**

```python
# Before
with open(a) as first:
    with open(b) as second:

# After
with open(a) as first, open(b) as second:
```

- [ ] **Step 4: Fix PLC0415 (import outside top level)**

18 findings. Some are deliberate — breaking an import cycle, or deferring an
expensive import. Move the ones that are incidental to module scope; for the
deliberate ones add `# noqa: PLC0415` naming the cycle or the cost.

- [ ] **Step 5: Fix FBT001/FBT003 (boolean positional arguments)**

`runtime.py` has `async def pause(self, sandbox: Sandbox, memory: bool)`. Make
the flag keyword-only and update every call site:

```python
async def pause(self, sandbox: Sandbox, *, memory: bool) -> None:
```

Find the call sites with `grep -rn "\.pause(" src/ tests/` before changing the
signature.

- [ ] **Step 6: Fix C901 / PLR0912 / PLR0915 / PLR0913 (complexity)**

Four complex functions, one over-branchy, one over-long, three with too many
arguments. These are genuine refactors — extract a helper with a clear name
rather than raising the thresholds. If a function resists decomposition after a
real attempt, `# noqa: C901` with a reason is acceptable; raising the global
threshold in config is not, because it silently exempts every future function.

- [ ] **Step 7: Fix the remainder**

`T201` (5 `print` calls — use logging, or `# noqa` in `scripts/` where stdout is
the interface), `PT018` (4 composite assertions — split into one assert each),
`PTH100`/`PTH108`/`PTH123` (use `pathlib`), `FURB162`, `PYI034`, `ARG001`,
`D401`, `D301`, `FLY002`, `PERF401`, `PT011`, `TRY301`.

- [ ] **Step 8: Completion command — this is the whole point of Tasks 5–9**

Run: `uv run ruff check .`
Expected: `All checks passed!` — zero findings under `select = ["ALL"]` with
the documented ignore list.

- [ ] **Step 9: Verify**

Run: `uv run pytest && uv run mypy`
Expected: 115 tests pass, mypy exits zero.

- [ ] **Step 9: Commit**

```bash
git add .
git commit -m "Clear the remaining strict-lint findings to zero"
```

---

### Task 10: Make the lint gate blocking

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Remove the lint deferral**

Delete `continue-on-error: true` from the `Lint` step only. The coverage step
keeps its deferral until Task 20.

- [ ] **Step 2: Prove the gate actually fails**

A gate nobody has seen fail is a gate nobody knows works.

```bash
printf '\nimport os\n' >> src/harborbox/security.py
uv run ruff check . ; echo "exit=$?"
git checkout src/harborbox/security.py
```

Expected: a non-zero exit reporting `F401` unused-import. Then confirm
`uv run ruff check .` is clean again after the checkout.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "Make the strict lint gate blocking"
```

---

### Task 11: Shared test fixtures — the Docker fake and real Postgres

**Files:**
- Create: `tests/fakes/__init__.py`, `tests/fakes/docker.py`
- Modify: `tests/conftest.py` (created in Task 2; extend it, keep the existing `client` fixture)

**Interfaces:**
- Consumes: nothing.
- Produces: fixtures `fake_docker` (a `FakeDockerClient`), `settings` (a `Settings` pointing at the fake), `pg_sessions` (an `async_sessionmaker[AsyncSession]` against real Postgres), and the `client` fixture the e2e tests in Task 2 duplicated.

This is the foundation for Tasks 12–19. Get it right before writing test bodies
against it.

- [ ] **Step 1: Enumerate the Docker surface actually used**

Run:

```bash
grep -nE "self\.client\.[a-z_]+|container\.[a-z_]+" src/harborbox/runtime.py | sort -u
```

The fake must implement exactly what this prints and nothing more — an
invented method is a fake that lies. Expect `client.info`, `client.close`,
`client.containers.get/run/list`, `client.networks.get`, and on a container
`start`, `stop`, `kill`, `pause`, `unpause`, `remove`, `reload`, `attrs`,
`status`, `wait`, `logs`.

- [ ] **Step 2: Write the fake**

Create `tests/fakes/__init__.py` (empty) and `tests/fakes/docker.py`:

```python
"""A hand-written stand-in for the slice of the Docker SDK runtime.py uses.

Hand-written rather than unittest.mock so the tests assert behaviour — a
container that has been killed reports "exited", a missing one raises NotFound —
instead of asserting that the code calls the library the way it already does.
Matches the house pattern in tests/test_warm_pool.py.
"""

from __future__ import annotations

from typing import Any

from docker.errors import NotFound


class FakeContainer:
    def __init__(self, container_id: str, name: str, status: str = "running") -> None:
        self.id = container_id
        self.name = name
        self.status = status
        self.attrs: dict[str, Any] = {
            "State": {"Status": status, "OOMKilled": False, "ExitCode": 0},
            "NetworkSettings": {"Networks": {}, "Ports": {}},
        }
        self.killed = False
        self.removed = False
        self.paused = False

    def reload(self) -> None:
        self.attrs["State"]["Status"] = self.status

    def start(self) -> None:
        self.status = "running"
        self.reload()

    def stop(self, timeout: int | None = None) -> None:
        self.status = "exited"
        self.reload()

    def kill(self, signal: str | None = None) -> None:
        self.killed = True
        self.status = "exited"
        self.reload()

    def pause(self) -> None:
        self.paused = True
        self.status = "paused"
        self.reload()

    def unpause(self) -> None:
        self.paused = False
        self.status = "running"
        self.reload()

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def wait(self, timeout: int | None = None) -> dict[str, int]:
        return {"StatusCode": self.attrs["State"]["ExitCode"]}

    def logs(self, **kwargs: Any) -> bytes:
        return b""

    def mark_oom_killed(self, exit_code: int = 137) -> None:
        """Put the container in the state runtime.py must read as OOM."""
        self.status = "exited"
        self.attrs["State"] = {"Status": "exited", "OOMKilled": True, "ExitCode": exit_code}


class FakeContainerCollection:
    def __init__(self) -> None:
        self.items: dict[str, FakeContainer] = {}
        self.run_kwargs: list[dict[str, Any]] = []

    def get(self, key: str) -> FakeContainer:
        for container in self.items.values():
            if key in (container.id, container.name):
                return container
        raise NotFound(f"no such container: {key}")

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self.run_kwargs.append({"image": image, **kwargs})
        name = kwargs.get("name", f"container-{len(self.items)}")
        container = FakeContainer(f"id-{len(self.items)}", name)
        self.items[container.id] = container
        return container

    def list(self, **kwargs: Any) -> list[FakeContainer]:
        return list(self.items.values())


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected: list[str] = []

    def connect(self, container: Any, **kwargs: Any) -> None:
        self.connected.append(getattr(container, "id", str(container)))


class FakeNetworkCollection:
    def __init__(self) -> None:
        self.items: dict[str, FakeNetwork] = {}

    def get(self, name: str) -> FakeNetwork:
        if name not in self.items:
            raise NotFound(f"no such network: {name}")
        return self.items[name]


class FakeDockerClient:
    def __init__(self, mem_total_bytes: int = 16 * 1024**3) -> None:
        self.containers = FakeContainerCollection()
        self.networks = FakeNetworkCollection()
        self.closed = False
        self._mem_total = mem_total_bytes

    def info(self) -> dict[str, Any]:
        return {"MemTotal": self._mem_total}

    def close(self) -> None:
        self.closed = True
```

Adjust to match exactly what Step 1 printed. If Step 1 shows a method not
listed here, add it; if it shows one of these is unused, delete it.

- [ ] **Step 3: Extend the conftest**

`tests/conftest.py` already exists from Task 2 with the `client` fixture. Add
the imports and the fixtures below to it; do not remove `client`.

```python
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import docker
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from harborbox.config import Settings
from harborbox.db import Base
from harborbox.runtime import DockerRuntime
from tests.fakes.docker import FakeDockerClient


# The `client` fixture from Task 2 stays exactly as it is, above this point.


@pytest.fixture
def fake_docker() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def settings() -> Settings:
    return Settings(total_memory_mb=16_384)


@pytest.fixture
def docker_runtime(
    fake_docker: FakeDockerClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> DockerRuntime:
    """A DockerRuntime whose SDK client is the fake.

    DockerRuntime.__init__ calls docker.DockerClient(...), which would try to
    reach a real daemon, so the constructor is patched rather than the attribute
    reassigned after the fact.
    """
    monkeypatch.setattr(docker, "DockerClient", lambda **_: fake_docker)
    return DockerRuntime(settings)


@pytest_asyncio.fixture
async def pg_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session factory against real Postgres.

    postgres_pool_store.py uses dialects.postgresql.insert and
    with_for_update(skip_locked=True); sqlite+aiosqlite cannot execute either,
    so there is no in-process substitute. Skips when no database is configured
    so a developer without Docker still gets a useful local run — CI always sets
    HARBORBOX_TEST_DATABASE_URL, so the 100% gate is never skipped there.
    """
    url = os.environ.get("HARBORBOX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HARBORBOX_TEST_DATABASE_URL is not set; start Postgres to run these")

    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
```

- [ ] **Step 4: Document how to run the Postgres tests locally**

Add to `README.md` under the existing development section:

````markdown
### Running the tests

```bash
uv sync --extra dev
uv run pytest
```

`postgres_pool_store.py` is tested against real Postgres, because it uses
`ON CONFLICT` and `FOR UPDATE SKIP LOCKED` that SQLite cannot execute. Those
tests skip unless a database is configured:

```bash
docker run -d --name harborbox-test-pg -p 5432:5432 \
  -e POSTGRES_USER=harborbox -e POSTGRES_PASSWORD=harborbox \
  -e POSTGRES_DB=harborbox_test postgres:17-alpine

export HARBORBOX_TEST_DATABASE_URL=postgresql+asyncpg://harborbox:harborbox@127.0.0.1:5432/harborbox_test
uv run pytest
```
````

- [ ] **Step 5: Write a smoke test proving both fixtures work**

Create `tests/test_fixtures.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.fakes.docker import FakeDockerClient


def test_fake_docker_reports_a_killed_container_as_exited(fake_docker: FakeDockerClient) -> None:
    container = fake_docker.containers.run("harborbox-sandbox:local", name="hb-1")

    container.kill()

    assert container.status == "exited"
    assert fake_docker.containers.get("hb-1").killed


def test_fake_docker_raises_not_found_for_an_unknown_container(fake_docker: FakeDockerClient) -> None:
    from docker.errors import NotFound

    with pytest.raises(NotFound):
        fake_docker.containers.get("missing")


@pytest.mark.postgres
async def test_postgres_fixture_yields_a_working_session(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_sessions() as session:
        assert (await session.execute(text("select 1"))).scalar_one() == 1
```

- [ ] **Step 6: Run without Postgres**

Run: `uv run pytest tests/test_fixtures.py -v`
Expected: two pass, the Postgres one skips with the configured reason.

- [ ] **Step 7: Run with Postgres**

Start the container from Step 4, export the URL, then:
Run: `uv run pytest tests/test_fixtures.py -v`
Expected: all three pass.

- [ ] **Step 8: Verify lint and types**

Run: `uv run mypy && uv run ruff check .`
Expected: both exit zero. This is new code under the strict gate from Task 10 —
it must arrive clean.

- [ ] **Step 9: Commit**

```bash
git add tests/ README.md
git commit -m "Add the Docker fake and the real-Postgres session fixture"
```

---

### Tasks 12–19: Coverage burn-down

Each task drives one area from its current coverage to 100%. They share a shape,
given once here rather than repeated eight times:

1. Run the module's current report to see exactly which lines are uncovered:
   `uv run pytest --cov=<import.path> --cov-report=term-missing`
2. Write tests in `tests/test_<module>.py`, using the fixtures from Task 11 and
   the house style from `tests/test_warm_pool.py` — plain typed fakes, one
   behaviour per test, a name that states the behaviour.
3. Assert behaviour, not implementation. `assert container.status == "exited"`,
   not `assert client.containers.get.called`.
4. **Completion command:** `uv run pytest --cov=<import.path> --cov-fail-under=100`
   must exit zero.
5. `uv run mypy && uv run ruff check .` must exit zero — new tests are under the
   strict gate.
6. Commit.

Do not add `# pragma: no cover` to reach the number. If a line is genuinely
unreachable, that is a finding about the code worth raising, not a line to
annotate.

---

### Task 12: Cover `harborbox/runtime.py` (174 uncovered)

**Files:**
- Create: `tests/test_runtime.py`
- Consumes: `docker_runtime`, `fake_docker`, `settings` from Task 11.

- [ ] **Step 1: See what is uncovered**

Run: `uv run pytest --cov=harborbox.runtime --cov-report=term-missing`
Expected: 22%, with ranges including 80-165 (`_start_sandbox_sync`), 188-206
(`wait_until_ready`), 226-237, 240-246, 251-257 (the agent HTTP helpers),
265-275 through 300-312 (file operations), 315-317, 320-329 (pause/resume),
332-345 (kill), 348-359 (`container_status`), 362-376 (`_raise_container_failure`).

- [ ] **Step 2: Write the first failing test**

```python
from __future__ import annotations

import pytest

from harborbox.models import Sandbox
from harborbox.runtime import DockerRuntime, SandboxMemoryExceededError


def sandbox(**overrides: object) -> Sandbox:
    values: dict[str, object] = {"id": "sbx-1", "container_name": "harborbox-sbx-1"}
    values.update(overrides)
    return Sandbox(**values)  # type: ignore[arg-type]


async def test_total_memory_prefers_the_configured_override(docker_runtime: DockerRuntime) -> None:
    assert await docker_runtime.total_memory_mb() == 16_384


async def test_total_memory_falls_back_to_the_daemon_report(
    docker_runtime: DockerRuntime,
) -> None:
    docker_runtime.settings.total_memory_mb = None

    assert await docker_runtime.total_memory_mb() == 16 * 1024


async def test_an_oom_killed_container_raises_memory_exceeded(
    docker_runtime: DockerRuntime, fake_docker
) -> None:
    container = fake_docker.containers.run("img", name="harborbox-sbx-1")
    container.mark_oom_killed()

    with pytest.raises(SandboxMemoryExceededError):
        await docker_runtime._raise_container_failure(sandbox())
```

Adjust `Sandbox(...)` to the real required fields — read `src/harborbox/models.py`
first. Note `SandboxMemoryExceededError` carries the `Error` suffix added in
Task 8.

- [ ] **Step 3: Run and confirm they fail for the right reason**

Run: `uv run pytest tests/test_runtime.py -v`
Expected: failures naming missing `Sandbox` fields or an assertion mismatch —
not `ImportError`. Fix the fixture until they pass.

- [ ] **Step 4: Work through the uncovered ranges from Step 1**

Cover in this order, largest first: `_start_sandbox_sync` (the container
creation kwargs — assert on `fake_docker.containers.run_kwargs` that memory,
CPU and labels are passed as configured), the agent HTTP helpers (use
`httpx.MockTransport` to serve canned agent responses rather than faking
`httpx.AsyncClient` itself), the file operations, then pause/resume/kill, then
`container_status` including the `NotFound` path.

- [ ] **Step 5: Completion command**

Run: `uv run pytest --cov=harborbox.runtime --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 6: Verify the whole suite, lint and types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`
Expected: all exit zero.

- [ ] **Step 7: Commit**

```bash
git add tests/test_runtime.py
git commit -m "Cover the Docker runtime against a fake daemon"
```

---

### Task 13: Cover `harborbox/postgres_pool_store.py` (160 uncovered)

**Files:**
- Create: `tests/test_postgres_pool_store.py`
- Consumes: `pg_sessions` from Task 11.

Every test in this file carries `@pytest.mark.postgres`.

- [ ] **Step 1: See what is uncovered**

Run: `HARBORBOX_TEST_DATABASE_URL=... uv run pytest --cov=harborbox.postgres_pool_store --cov-report=term-missing`

- [ ] **Step 2: Write the first failing test**

```python
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborbox.postgres_pool_store import AsyncPostgresPoolStateStore

pytestmark = pytest.mark.postgres


async def test_put_idle_then_take_idle_returns_the_same_sandbox(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = AsyncPostgresPoolStateStore(pg_sessions)
    await store.set_max_idle("relaydeck", 2)

    await store.put_idle("relaydeck", "sbx-1")

    assert await store.try_take_idle("relaydeck") == "sbx-1"
    assert await store.try_take_idle("relaydeck") is None


async def test_a_taken_sandbox_is_not_handed_to_a_second_caller(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """This is what SKIP LOCKED is for, and what a fake session cannot prove."""
    store = AsyncPostgresPoolStateStore(pg_sessions)
    await store.set_max_idle("relaydeck", 4)
    await store.put_idle("relaydeck", "sbx-1")
    await store.put_idle("relaydeck", "sbx-2")

    first = await store.try_take_idle("relaydeck")
    second = await store.try_take_idle("relaydeck")

    assert {first, second} == {"sbx-1", "sbx-2"}
```

- [ ] **Step 3: Run and confirm**

Run: `uv run pytest tests/test_postgres_pool_store.py -v` with the URL exported.
Expected: pass. Without the URL: skipped.

- [ ] **Step 4: Cover the rest**

The primary-lock trio (`try_acquire_primary_lock`, `renew_primary_lock`,
`release_primary_lock`) including contention and expiry; the reaper paths
(`reap_expired_idle`, `reap_expired_idle_min_ttl`); the snapshots
(`snapshot_counters`, `snapshot_idle_entries`); the destroy lifecycle
(`get_destroy_state`, `begin_destroy`, `mark_destroyed`, `clear_pool_state`);
`set_idle_entry_ttl` / `get_max_idle`; and every validator
(`_validate_pool_name`, `_validate_owner`, `_validate_owner_ttl`) including
their rejection paths, plus `_require_active`.

Write at least one genuinely concurrent test — two `try_take_idle` calls via
`asyncio.gather` against separate sessions — since serial calls do not exercise
`skip_locked`.

- [ ] **Step 5: Completion command**

Run: `HARBORBOX_TEST_DATABASE_URL=... uv run pytest --cov=harborbox.postgres_pool_store --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 6: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 7: Commit**

```bash
git add tests/test_postgres_pool_store.py
git commit -m "Cover the Postgres pool store against a real database"
```

---

### Task 14: Cover `harborbox/scheduler.py` (253 uncovered)

**Files:**
- Create: `tests/test_scheduler.py` (extends the existing `tests/test_scheduler_slots.py` coverage)

- [ ] **Step 1: See what is uncovered**

Run: `uv run pytest --cov=harborbox.scheduler --cov-report=term-missing`
Expected: 21%, uncovered ranges 96-137, 154-264, 269-277, 284-398, 401-454,
457-482, 490-531, 534-562, 599-605.

- [ ] **Step 2: Read the module and list its behaviours**

Before writing tests, read `src/harborbox/scheduler.py` and write down each
public method and the decisions it makes. The scheduler is the most
behaviour-dense module in the repo; tests written without that map will chase
lines rather than behaviours.

- [ ] **Step 3: Write tests behaviour-by-behaviour**

Follow `tests/test_scheduler_slots.py` for the existing fixture style and
extend it. Cover admission decisions, queueing and dequeueing, slot accounting,
capacity headroom (the subject of commit c304261), and every error path.

- [ ] **Step 4: Completion command**

Run: `uv run pytest --cov=harborbox.scheduler --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 5: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "Cover the scheduler's admission, queueing and slot accounting"
```

---

### Task 15: Cover `harborbox/api.py` (267 uncovered)

**Files:**
- Create: `tests/test_api.py`

- [ ] **Step 1: See what is uncovered**

Run: `uv run pytest --cov=harborbox.api --cov-report=term-missing`
Expected: 43%, a long list of route bodies from 485 to 1205.

- [ ] **Step 2: Write the first failing test using FastAPI's TestClient**

`api.py` is a FastAPI app, so its routes are reachable through the ASGI stack
with dependency overrides — no fake HTTP layer needed.

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from harborbox.api import app
from harborbox.runtime_protocol import StartedSandbox


class FakeRuntime:
    """Implements the slice of the runtime protocol the API routes call."""

    def __init__(self) -> None:
        self.killed: list[str] = []

    async def start_sandbox(self, sandbox: object) -> StartedSandbox:
        return StartedSandbox(container_id="id-1", host="127.0.0.1", port=49000)

    async def kill(self, sandbox: object) -> None:
        self.killed.append(getattr(sandbox, "id", ""))


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_reachable_without_a_key(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_creating_a_sandbox_without_a_key_is_rejected(client: TestClient) -> None:
    assert client.post("/v1/sandboxes", json={"memory_mb": 128}).status_code == 401
```

Read `api.py` for the real dependency names and use
`app.dependency_overrides[...]` to inject `FakeRuntime` and a test session.
`StartedSandbox`'s real field names are in `src/harborbox/runtime_protocol.py` —
check them before using the constructor above.

- [ ] **Step 3: Cover every route**

One test per route per outcome: success, unauthenticated, not-found, and each
validation failure. The auth paths matter most — `security.py` is at 50% and
this is what exercises it.

- [ ] **Step 4: Completion command**

Run: `uv run pytest --cov=harborbox.api --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 5: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add tests/test_api.py
git commit -m "Cover every API route including its auth and validation paths"
```

---

### Task 16: Cover the OpenSandbox runtime and compat layers (252 uncovered)

**Files:**
- Create: `tests/test_opensandbox_compat.py`; extend `tests/test_opensandbox_runtime.py`

- [ ] **Step 1: See what is uncovered**

Run: `uv run pytest --cov=harborbox.opensandbox_runtime --cov=harborbox.opensandbox_compat --cov-report=term-missing`
Expected: 46% and 51%.

- [ ] **Step 2: Extend the existing suite**

`tests/test_opensandbox_runtime.py` already exists — read it and follow its
fakes rather than inventing new ones.

- [ ] **Step 3: Cover the compat shims**

`opensandbox_compat.py` exists to absorb differences between opensandbox
versions, so its tests should assert each shim's behaviour on both shapes it
adapts. Read the module to find what those shapes are.

- [ ] **Step 4: Completion command**

Run: `uv run pytest --cov=harborbox.opensandbox_runtime --cov=harborbox.opensandbox_compat --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 5: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "Cover the OpenSandbox runtime and its version compatibility shims"
```

---

### Task 17: Cover `harborbox_agent` (280 uncovered)

**Files:**
- Create: `tests/test_agent_main.py`, `tests/test_agent_kernel.py`, `tests/test_agent_output.py`

All three modules are at 0%.

- [ ] **Step 1: Cover `output.py` first (14 statements)**

Smallest and dependency-free — `OutputBudget` is pure logic. Start here to
establish the pattern.

- [ ] **Step 2: Cover `main.py` with TestClient (168 statements)**

It is a FastAPI app with routes `/health`, `/v1/execute`, `/v1/commands`,
`/v1/processes`, `/v1/files`, `/v1/files/content`, `/v1/files/list`, and a
DELETE on files. Same `TestClient` approach as Task 15, with the
`authenticate` dependency overridden. `lifespan` starts a `KernelSession`;
override it with a fake so tests do not need a real kernel.

- [ ] **Step 3: Cover `kernel.py` with a fake kernel client (98 statements)**

`jupyter_client` is imported only under `if TYPE_CHECKING:` and is not a
harborbox dependency — it ships inside the sandbox image. So `AsyncKernelManager`
and `AsyncKernelClient` are duck-typed at runtime and a fake substitutes without
adding the dependency:

```python
class FakeKernelClient:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = list(messages)

    async def get_iopub_msg(self, timeout: float | None = None) -> dict[str, object]:
        if not self._messages:
            raise asyncio.TimeoutError
        return self._messages.pop(0)

    def execute(self, code: str) -> str:
        return "msg-1"
```

Build message sequences that reproduce each branch of `_execute_locked`:
`stream` stdout, `stream` stderr, `execute_result`, `display_data`, `error`,
and the truncation path where `OutputBudget` trips.

- [ ] **Step 4: Completion command**

Run: `uv run pytest --cov=harborbox_agent --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 5: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "Cover the sandbox agent's routes, kernel session and output budget"
```

---

### Task 18: Cover `harborbox_sdk` (65 uncovered)

**Files:**
- Create: `tests/test_sdk_client.py`; extend `tests/test_sdk_models.py`

- [ ] **Step 1: See what is uncovered**

Run: `uv run pytest --cov=harborbox_sdk --cov-report=term-missing`
Expected: `client.py` 52%, `models.py` 64%.

- [ ] **Step 2: Cover the client with `httpx.MockTransport`**

The SDK is an httpx wrapper, so a mock transport gives real request/response
plumbing without a server:

```python
import httpx

from harborbox_sdk import SandboxClient


def test_the_api_key_is_sent_on_every_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "sbx-1", "status": "running"})

    transport = httpx.MockTransport(handler)
    with SandboxClient(api_key="k", transport=transport) as client:
        client.sandboxes.create(memory_mb=128)

    assert seen[0].headers["authorization"] == "Bearer k"
```

Read `src/harborbox_sdk/client.py` first — if it does not accept a `transport`
argument, adding one is a reasonable, small change that makes the SDK testable,
and is preferable to monkeypatching `httpx.Client`.

- [ ] **Step 3: Finish `models.py`**

Extend `tests/test_sdk_models.py`. The uncovered ranges are mostly property
accessors and `from_dict`-style constructors — cover each field's present and
absent cases.

- [ ] **Step 4: Completion command**

Run: `uv run pytest --cov=harborbox_sdk --cov-fail-under=100`
Expected: exit zero.

- [ ] **Step 5: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 6: Commit**

```bash
git add tests/ src/harborbox_sdk/
git commit -m "Cover the SDK client transport and model accessors"
```

---

### Task 19: Cover the remaining modules (~185 uncovered)

**Files:**
- Create: `tests/test_presenters.py`, `tests/test_db.py`, `tests/test_security.py`, `tests/test_runtime_factory.py`, `tests/test_config.py`
- Modify: `tests/test_templates.py`, `tests/test_warm_pool.py`, `tests/test_reaper.py`, `tests/test_execution_secrets.py`

**Scope:** `template_builder.py` (61), `warm_pool.py` (43), `reaper.py` (41),
`presenters.py` (9), `config.py` (8), `db.py` (5), `execution_secrets.py` (4),
`security.py` (4), `runtime_factory.py` (4), `schemas.py` (3), `templates.py`
(2), `main.py` (2).

- [ ] **Step 1: Take the small ones first**

`schemas.py` (3), `templates.py` (2), `main.py` (2), `execution_secrets.py` (4),
`security.py` (4), `runtime_factory.py` (4), `db.py` (5), `config.py` (8) —
about 32 statements total, mostly single branches in modules already near 100%.
Run `uv run pytest --cov=harborbox --cov-report=term-missing` and work down the
`Missing` column.

`db.py` needs care: it builds its engine at module import via `get_settings()`,
so tests must not re-import it with different settings. Cover `create_schema`
and `get_session` against the `pg_sessions` engine from Task 11.

`main.py`'s two statements are the uvicorn entrypoint. If both fall inside
`if __name__ == "__main__":` they are already excluded by the Task 1 config and
the module will read 100% — confirm rather than assume.

- [ ] **Step 2: Then `template_builder.py`, `warm_pool.py` and `reaper.py`**

These have existing suites to extend. `template_builder.py` shells out to Docker
build — use the `fake_docker` fixture from Task 11.

- [ ] **Step 3: Completion command**

Postgres must be running and exported, or the Task 13 tests skip and the gate
cannot reach 100%:

```bash
export HARBORBOX_TEST_DATABASE_URL=postgresql+asyncpg://harborbox:harborbox@127.0.0.1:5432/harborbox_test
uv run pytest --cov --cov-fail-under=100
```

Expected: exit zero. This is the whole-repo gate, not a per-module one — after
this task, coverage is 100% across `src/`.

- [ ] **Step 4: Verify, lint, types**

Run: `uv run pytest && uv run mypy && uv run ruff check .`

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "Cover the remaining modules to reach 100%"
```

---

### Task 20: Make the coverage gate blocking

**Files:**
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`

- [ ] **Step 1: Set the threshold**

Add to `[tool.coverage.report]` in `pyproject.toml`:

```toml
fail_under = 100
```

- [ ] **Step 2: Remove the last deferral**

Delete `continue-on-error: true` from the `Tests and coverage` step in
`.github/workflows/ci.yml`. No `continue-on-error` should remain on any gate —
the `Lint backlog (non-blocking)` step keeps its `|| true`, which is correct and
deliberate.

Run: `grep -n "continue-on-error" .github/workflows/ci.yml`
Expected: only the `download-artifact` steps in the `report` job.

- [ ] **Step 3: Prove the gate fails**

```bash
cat >> src/harborbox/presenters.py <<'PY'


def unreached_helper() -> str:
    return "this line has no test"
PY
uv run pytest --cov ; echo "exit=$?"
git checkout src/harborbox/presenters.py
```

Expected: non-zero exit, with coverage reporting below 100 and naming the new
line. Then confirm `uv run pytest --cov` exits zero again after the checkout.

- [ ] **Step 4: Full green verification**

Run:

```bash
uv run ruff check . && uv run mypy && HARBORBOX_TEST_DATABASE_URL=postgresql+asyncpg://harborbox:harborbox@127.0.0.1:5432/harborbox_test uv run pytest --cov
```

Expected: all three exit zero — zero lint findings under `select = ["ALL"]`,
clean strict types, every test passing, 100% coverage.

- [ ] **Step 5: Confirm the report shows the finished state**

```bash
uv run pytest --cov --cov-report=json --json-report --json-report-file=unit-results.json
uv run ruff check --select ALL --output-format=json -o ruff-strict.json . || true
uv run python scripts/ci_report.py
```

Expected: three package rows all at 100.0%, `Total unit test coverage: 100.0%`,
and a `Lint backlog` block containing only the deliberately-ignored families
(docstrings, copyright, trailing comma, FastAPI conventions) — roughly 1080,
with zero findings from any enforced rule.

- [ ] **Step 6: Commit and push**

```bash
git add pyproject.toml .github/workflows/ci.yml
git commit -m "Make the 100% coverage gate blocking"
git push
```

- [ ] **Step 7: Confirm CI is green and the comment is correct**

Check the pull request. Both gates must be green, the sticky comment present,
and its numbers must match the job logs. Push once more (an empty commit is
fine) and confirm the comment is **updated in place** rather than duplicated —
that is the one behaviour of the sticky comment that only a second push can
verify.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Packages as report rows | 3 (`PACKAGES`) |
| Ruff `ALL` minus documented ignores | 5 |
| Backlog cleared to zero | 6, 7, 8, 9 |
| Non-blocking `--select ALL` backlog reported | 4 (step), 3 (rendering) |
| Coverage 100% with three documented exclusions | 1 (config), 20 (threshold) |
| Fake Docker client for `runtime.py` | 11, 12 |
| Real Postgres for `postgres_pool_store.py` | 11 (fixture), 13 |
| `harborbox_agent` covered, not omitted | 17 |
| E2E converted to pytest | 2 |
| `scripts/ci_report.py` + its own tests | 3 |
| Three-job workflow, sticky comment, `pull-requests: write` | 4 |
| Postgres service container | 4 |
| Python version pinning | 1 |

No gaps.

**Placeholder scan:** No TBD/TODO. Every code step carries real code. The
burn-down tasks (6–9, 12–19) deliberately give a pattern plus an objective
completion command rather than inlining 444 fixes and ~1600 tests; the "How to
read the burn-down tasks" section states that contract explicitly.

**Type consistency:** `render_report(root: Path) -> str` is consistent between
Tasks 3 and 4. `FakeDockerClient`, `FakeContainer`, `FakeContainerCollection`,
`FakeNetwork` are defined in Task 11 and used under those names in 12, 16 and
19. The fixtures `fake_docker`, `settings`, `docker_runtime`, `pg_sessions` are
defined in Task 11 and referenced by those names throughout. `pytest.mark.e2e`
and `pytest.mark.postgres` are registered in Task 1 and used in Tasks 2, 11 and
13. `SandboxUnavailableError` / `SandboxMemoryExceededError` carry the `Error`
suffix from Task 8 onward, and Task 12 uses the renamed form — Tasks 6 and 12
were reconciled on this point.

## Open risks carried from the spec

1. **The `onvo-ci` runner may not be reachable from harborbox.** Task 4 Step 5
   is the explicit checkpoint. A queued-forever job is the failure mode, and it
   is silent.
2. **The runner is 2 cores / 3.7G.** The e2e job builds four images and runs a
   container-spawning stack. Task 4's 40-minute timeout is a guess that will
   need tuning after the first real run.
3. **`secrets.HARBORBOX_E2E_API_KEY` does not exist yet.** The e2e job needs it
   set on the repository before it can pass. This is a user action, not a code
   change.
4. **100% is a standing cost.** Once Task 20 lands, every future pull request
   must arrive fully covered.
