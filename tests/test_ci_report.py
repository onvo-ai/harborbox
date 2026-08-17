from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ci_report import render_report

if TYPE_CHECKING:
    from pathlib import Path


def write(root: Path, name: str, payload: object) -> None:
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def test_renders_a_row_per_package_with_coverage_and_counts(tmp_path: Path) -> None:
    write(
        tmp_path,
        "coverage.json",
        {
            "files": {
                "src/harborbox/api.py": {"summary": {"covered_lines": 8, "num_statements": 10}},
                "src/harborbox_agent/main.py": {
                    "summary": {"covered_lines": 3, "num_statements": 3}
                },
            }
        },
    )
    write(tmp_path, "unit-results.json", {"summary": {"total": 12, "passed": 12, "failed": 0}})

    report = render_report(tmp_path)

    assert "| `harborbox` | 12 | 80.0% |" in report
    assert "`harborbox_agent`" in report
    assert "100.0%" in report
    assert "Total unit test coverage: 84.6%" in report


def test_missing_artifacts_render_as_dashes_not_zeros(tmp_path: Path) -> None:
    report = render_report(tmp_path)

    assert "| `harborbox` | — | — | — |" in report
    assert "Unit test results unavailable." in report
    assert "Total unit test coverage: —." in report


def test_missing_e2e_results_keep_internal_capitals(tmp_path: Path) -> None:
    report = render_report(tmp_path)

    assert "E2E test results unavailable." in report
    assert "E2e" not in report


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
