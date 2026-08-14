# Strict lint, full unit coverage, and a CI results report

Date: 2026-08-14
Status: approved, not yet implemented

## Goal

Three things, delivered on one branch that is green when it merges:

1. Ruff running a strict rule set with every finding cleared to zero, blocking.
2. 100% unit-test line coverage over `src/`, blocking.
3. A CI job that posts a sticky per-package tests-and-coverage comment on every
   pull request, modelled on the report onvo already publishes.

## Starting point

harborbox has no `.github/` directory. Nothing runs on push or on a pull
request today.

Local tooling is in better shape than that suggests:

| Check | State |
|---|---|
| `ruff check .` (current 6 families) | passes |
| `mypy` (strict, 25 files) | passes |
| `pytest` | 115 tests, all passing |
| Line coverage over `src/` | **49%** — 1643 of 3239 statements uncovered |
| `ruff check --select ALL` | **1524 findings** |

Uncovered statements concentrate in six modules:

| Module | Uncovered | Coverage |
|---|---:|---:|
| `harborbox/api.py` | 267 | 43% |
| `harborbox/scheduler.py` | 253 | 21% |
| `harborbox/opensandbox_runtime.py` | 185 | 46% |
| `harborbox/runtime.py` | 174 | 22% |
| `harborbox_agent/main.py` | 168 | 0% |
| `harborbox/postgres_pool_store.py` | 160 | 21% |

The rest is spread across `opensandbox_compat.py` (67), `template_builder.py`
(61), `harborbox_sdk/models.py` (50), `warm_pool.py` (43), `reaper.py` (41),
`harborbox_agent/kernel.py` (98) and `output.py` (14).

## Design

### Packages

harborbox is not a monorepo, but it ships three importable packages —
`harborbox`, `harborbox_agent`, `harborbox_sdk`. These are the rows of the
report table, filling the role onvo's workspace packages fill.

### Ruff: `ALL` minus a documented ignore list

`[tool.ruff.lint]` becomes `select = ["ALL"]` with an ignore list in which every
entry carries a one-line reason. The point of inverting the default is that an
exemption becomes a decision on record rather than an unexamined omission, and
new ruff rules arrive enabled.

Of the 1524 findings, roughly 1060 fall into families that get ignored wholesale:

| Family | Count | Reason to ignore |
|---|---:|---|
| `D1xx` undocumented-* | 472 | Docstring-on-everything is not this repo's convention. |
| `S101` assert | 262 | `per-file-ignores` for `tests/` only — pytest's idiom. Stays enforced in `src/`. |
| `COM812` missing-trailing-comma | 205 | Conflicts with the formatter. |
| `CPY001` missing-copyright | 49 | No copyright headers in this repo. |
| `FAST002` / `B008` | 72 | FastAPI's `Depends`/`Query` call-in-default is the framework convention. `B008` is already ignored today. |

The remaining **~460 findings are cleared to zero**, not ignored. The larger
groups there are `TRY003` (75), `PLR2004` magic values (40), `SLF001` private
access (40), `EM101`/`EM102` exception message style (75), `ANN401` (27),
`TC001`–`TC003` typing-only imports (41), `PLC0415` import-outside-top-level
(18) and `SIM117` nested `with` (16). `ASYNC240` keeps its existing targeted
ignore, which already carries a reason in the current config.

The workflow additionally runs `ruff check --select ALL` with *nothing* ignored
as a non-blocking step, and the report prints that number. That preserves onvo's
"the backlog is printed on every run rather than discovered by someone going
looking" property — here it should read as the ignored families and nothing else.

### Coverage: 100% with a short exclusion list

`[tool.coverage.report]` sets `fail_under = 100` with `exclude_lines` entries
that each carry a reason:

- `if TYPE_CHECKING:` — import-time-only blocks.
- `raise NotImplementedError` — `runtime_protocol.py` stubs.
- `if __name__ == "__main__":` — uvicorn/agent entrypoints.

Nothing else is excluded. `harborbox_agent/main.py` and `kernel.py`, both at 0%
today, are covered by real tests rather than omitted.

For the two wrapper modules — `runtime.py` over the Docker SDK and
`postgres_pool_store.py` over asyncpg, 334 uncovered statements between them —
coverage comes from **hand-written fakes** shared through `conftest.py`
fixtures: a fake Docker client and a fake asyncpg pool/connection. The
alternative, per-test `unittest.mock`, reaches the same number while mostly
asserting that the code calls the library the way it already calls it; fakes
let the tests assert behaviour and survive refactoring.

### E2E becomes pytest

`tests/e2e_smoke.py`, `e2e_oom.py`, `e2e_large_upload.py` and
`e2e_onvo_readiness.py` are currently standalone `__main__` scripts driven by
`HARBORBOX_API_KEY`. They produce no machine-readable result, so the report
cannot count them.

They convert to pytest tests marked `@pytest.mark.e2e`, with
`addopts = "-q -m 'not e2e'"` keeping them out of the unit run and out of the
coverage number. The e2e job runs `pytest -m e2e --json-report`.

### `scripts/ci_report.py`

A Python port of onvo's `scripts/ci-report.mjs`. Python rather than Node because
this repo has no Node toolchain and the runner should not need one.

It keeps the contract that makes the original safe: it reads only artifacts the
earlier steps already wrote, runs nothing itself, and therefore cannot fail a
build. Inputs, all optional:

| Path | Producer |
|---|---|
| `coverage.json` | `pytest --cov --cov-report=json` |
| `unit-results.json` | `pytest --json-report` |
| `ruff-strict.json` | `ruff check --select ALL --output-format=json` |
| `e2e-results.json` | `pytest -m e2e --json-report` |

A package with no data renders as `—` rather than being dropped, for the reason
onvo documents: a silently missing row reads as "fine" when it usually means the
step crashed.

Output columns: Package, Unit tests, Unit test coverage, E2E tests, Lint
backlog — followed by the totals line and the same `<details>` breakdown of the
top backlog rules.

### `.github/workflows/ci.yml`

Three jobs on `[self-hosted, onvo-ci]`, the runner onvo uses. GitHub-hosted is
not an option: onvo's ci.yml documents that the org's Actions spend is blocked
and hosted jobs fail in about two seconds.

- **`test`** — `uv sync --extra dev`, then `ruff check .`, `mypy`, and
  `pytest --cov --cov-report=json --json-report`, all blocking. Then the
  non-blocking `ruff --select ALL` backlog step. Uploads coverage, results and
  lint JSON.
- **`e2e`** — `docker compose build` and `up`, then `pytest -m e2e`. Uploads
  results. Blocking, per the decision to run E2E for real.
- **`report`** — `if: always()`, `needs: [test, e2e]`. Downloads both artifact
  sets, runs `scripts/ci_report.py`, appends to `$GITHUB_STEP_SUMMARY`, and
  upserts a sticky PR comment keyed on `<!-- harborbox-ci-report -->`. Needs
  `pull-requests: write`, which the default token does not have — without it the
  step 403s while everything else passes, which reads as a broken feature rather
  than a missing scope.

### Python version pinning

The repo has no `.python-version`, so `uv` resolves to whatever is newest
locally — 3.14.4 on this machine — while `requires-python` is `>=3.12` and both
ruff's `target-version` and mypy's `python_version` say 3.12. Lint and type
results therefore depend on who ran them. The branch adds a `.python-version`
so local and CI agree.

## Testing

The change is itself test infrastructure, so verification is the gates running
green on a real pull request:

- `ruff check .` exits zero with `select = ["ALL"]` and the documented ignores.
- `mypy` continues to exit zero.
- `pytest` reports 100% and `fail_under = 100` does not trip.
- The e2e job brings the stack up and its four tests pass.
- The report comment appears on the PR, is updated in place on a second push
  rather than duplicated, and its numbers match the job logs.

`scripts/ci_report.py` gets its own unit tests — it is `src`-adjacent code that
parses four JSON shapes, and its failure mode (a wrong number in a comment
everyone trusts) is quiet. Those tests cover the missing-file, malformed-file
and zero-tests paths.

## Risks

**The runner is 2 cores / 3.7G.** onvo's ci.yml records needing an 8G swapfile
there just for `tsc`. The e2e job builds four Docker images and then runs a
stack that spawns further containers. Python image builds are lighter than
`tsc` and layer-cache well, so this is expected to fit, but it is the part most
likely to need tuning after the first real run.

**Runner availability is unverified.** Listing org runners returns 403 with the
current token, so whether `onvo-ci` is org-level or scoped to the onvo repo is
unconfirmed. If it is repo-scoped, the workflow will queue forever rather than
fail loudly, and the label will need changing.

**100% is a ratchet that bites later.** Once `fail_under = 100` is on, every
future pull request must arrive fully covered. That is the intended effect, but
it is a standing cost on unrelated work, not a one-time push.
