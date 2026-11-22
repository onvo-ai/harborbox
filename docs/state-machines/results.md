# Harborbox state machines: results

What came of each hole in `docs/state-machines.md`. Each modelled machine has a TLA+ model in
`docs/state-machines/models/<ID>/`. `<ID>.cfg` checks the code as it was, and `<ID>-fixed.cfg`
checks it after the fix. `chart-before-fix.mmd` and `chart.mmd` are the two charts.

To check a model again:

```sh
cd docs/state-machines/models/H2
<pstack>/skills/statechart/scripts/statechart H2.tla --config H2-fixed.cfg --view view.mjs --out /tmp/h2-out
```

`<pstack>` is the pstack plugin folder. The script installs TLC and Java with mise on its first run.

## H1. Sandbox start against DELETE

| Rule | Verdict | Test |
|---|---|---|
| Once both have finished, a deleted sandbox has no container left running | proved and fixed | `tests/test_sandbox_delete_race.py` |
| A sandbox the caller deleted stays deleted | model only, fixed with the rule above | — |

Fix: DELETE commits `killed` before it calls the runtime, then rereads the row for a container
id a start saved. The start reads the row under `FOR UPDATE` before it saves `running`.

## H2. Idle ladder against admission

| Rule | Verdict | Test |
|---|---|---|
| The idle sweep never removes a container while a command runs in it | proved and fixed | `tests/test_idle_pause_race.py` |
| Going idle never costs the caller the sandbox | proved and fixed | same |
| Every snapshot that exists is one the row points at | proved and fixed | same |

Fix: a pause first claims the sandbox as `pausing`. The claim locks the sandbox row, then counts
active executions in a later statement. Admission locks the same rows and defers a command on a
`pausing` sandbox. The tier is written over the claim only, and a lost claim deletes the new
snapshot. The explicit pause route uses the same claim. A boot settles any `pausing` row by what
its container says. A second test holds an admission pass open on PostgreSQL and shows the claim
waits for it; it fails when the claim takes no row lock.

New state: `pausing`. It reserves memory and CPU, and file calls get 409 with `Retry-After: 1`.
It is in the API schema, the OpenSandbox status map (`Pausing`) and the TypeScript SDK type.

## H3. Reaper against a start

| Rule | Verdict | Test |
|---|---|---|
| The sweep reclaims only starts nobody is waiting on | proved and fixed | `tests/test_reaper_start_race.py` (2 tests) |
| Coming back from idle never fails because the sweep deleted the snapshot | proved and fixed | same |
| No container runs for a row that says it is gone | holds before and after | — |

Fix: a `starting` row is timed from `updated_at` or `last_activity_at`, whichever is later.
Each start stamps `updated_at` as it begins, so a retry restarts the clock. The sweep marks a row
`killed` with a write conditional on the status and `updated_at` it read, and kills after that.

## H4. Execution lifecycle

| Rule | Verdict | Test |
|---|---|---|
| Once its run task has ended, an execution is finished | proved and fixed | `tests/test_execution_lifecycle_races.py` |
| A sandbox stays `starting` only while a start for it is coming | proved and fixed | same |
| A cancel that answered "cancelled" stopped the command | proved and fixed | same |
| A finished execution keeps the outcome it first had | model only, fixed with the rule above | — |

Fix: `_mark_starting` fails or cancels an execution it refuses to start, and wakes its waiters.
Cancel is one `UPDATE ... WHERE status = 'queued'`.

## Not modelled

| # | Why not |
|---|---|
| H5 metadata blob | The lost renew needs the OpenSandbox compatibility API, which Toolspoke does not use. The fix is a per-key JSON update instead of writing the whole blob back. |
| H6 boot recovery | Correct for one replica. Failing active executions at boot is the documented contract. |
| H7 warm pool | The short-TTL hole depends on OpenSandbox's renew failing, which needs a live stack to reproduce. |
| H8 template delete and GC | Template DELETE has no check for sandboxes on the template. That is a missing guard, not a race, and Toolspoke never deletes templates. |
| H9 to H11 | Sound, or covered by H1 to H4. |
