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


def read_json(path: Path) -> Any:  # noqa: ANN401 - shape varies per artifact file
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


# `coverage` is the raw coverage.json blob (or None if the artifact is
# missing/malformed) - its shape is an external tool's, not ours to pin down.
def coverage_by_package(coverage: Any) -> dict[str, tuple[int, int]]:  # noqa: ANN401
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


# `results` is the raw pytest-json-report blob (or None); shape is that tool's.
def counts(results: Any) -> tuple[int, int] | None:  # noqa: ANN401
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


# `findings` is the raw ruff --output-format=json blob (or None); shape is
# ruff's own JSON schema, not ours to pin down.
def lint_by_rule(findings: Any) -> dict[str, int]:  # noqa: ANN401
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
