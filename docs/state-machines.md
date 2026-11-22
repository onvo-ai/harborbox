# Harborbox state machines

A map of every entity whose behaviour depends on a status or phase. Most of them have no one
enum: the state is spread over a status column, nullable columns, a JSON metadata blob,
in-process dictionaries and OpenSandbox's own view of the container.

Status: **mapped from code reading, 2026-11-22.** Each hole below is a claim by a code reader
until its TLA+ path replays as a failing test against the real code. The machines marked
"modelled" have a model in `docs/state-machines/models/<ID>/` and a verdict in
`docs/state-machines/results.md`. "Sure", "likely" and "guess" are the reader's confidence.

Paths are relative to the repo root. `scheduler.py` and `api.py` are in `src/harborbox/`.

## Sandboxes and executions

| # | Machine | Where the state lives | Worst hole | Confidence |
|---|---|---|---|---|
| H1 | Sandbox start and kill | `sandboxes.status`, `container_id`; `Scheduler._sandbox_start_locks`, `_pending_starts`; OpenSandbox container | `DELETE /v1/sandboxes/{id}` kills the runtime, then writes `killed` (`api.py:868-872`). A start that finishes between those two steps writes `running` with its container id, and DELETE then overwrites it with `killed` and `container_id = NULL`. The container runs on with no row pointing at it. | likely |
| H2 | Idle ladder (running → paused_memory → paused_cold) | `sandboxes.status`, `last_activity_at`, snapshot id in `metadata` | The sweep picks idle sandboxes in one read (`scheduler.py:1191-1202`) and pauses them later. Admission does not see the plan, so an execution admitted in between runs in a container that is snapshotted and killed under it. The execution fails, `_fail_execution` marks the sandbox `failed` (`scheduler.py:1020-1024`), and the new snapshot id is never stored. The caller's files are lost. | sure — modelled |
| H3 | Reaper sweep (`reaper.py`) | `sandboxes.status`, `last_activity_at`, `updated_at` | "Stuck in `starting`" is measured from `last_activity_at` (`reaper.py:163`), which for a sandbox resumed after its idle timeout is already older than the 300 s threshold. A sweep that lands during the resume kills it. The sweep also kills first and then writes `killed` over whatever the row holds now (`reaper.py:240-262`). | sure — modelled |
| H4 | Execution lifecycle | `executions.status`, `cancel_requested`; `Scheduler._running_tasks` | `_mark_starting` refuses an admitted execution whose sandbox was killed and returns without writing (`scheduler.py:725-730`). DELETE cancels only `queued` rows (`api.py:860-867`), so an execution admitted just before a DELETE stays `admitted` until the next restart. Cancel writes `cancelled` with no status check (`api.py:1384-1391`). | sure — modelled |
| H5 | Sandbox metadata blob | `sandboxes.metadata` (expiry, snapshot id, warm-pool flag, template) | Several actors copy the whole blob, call OpenSandbox, and write the copy back: the start path (`scheduler.py:958,987`), `_suspend` (`scheduler.py:1293`), the expiry sweep (`scheduler.py:1183`). A renew of `expires_at` that lands during a start is lost, and the expiry sweep kills the sandbox at the old time. | likely |
| H6 | Boot recovery | `executions.status`, `sandboxes.status`, `sandbox_templates.status` | `_recover_interrupted_jobs` marks every active execution `failed` (`scheduler.py:1296-1310`), but the process may still run in the container, and its result is lost. With more than one replica, one replica's boot fails another's live executions and builds. | sure (one replica: by design) |

## Pools and templates

| # | Machine | Where the state lives | Worst hole | Confidence |
|---|---|---|---|---|
| H7 | Warm pool | `warm_pool_states` (primary lease, `destroy_state`), `warm_pool_idle_sandboxes`; SDK in-memory owner | The renew after an acquire runs in the background and a failure is only logged (`warm_pool.py:181-209`). A sandbox taken with 31 s of pool TTL left keeps that TTL, and OpenSandbox expires it under the caller. A crash between acquire and the `running` write leaks the container for up to its TTL. Two lease owners and a double hand-out are not possible: both lock the state row. | likely |
| H8 | Template build | `sandbox_templates.status` (building, ready, failed), `image`, `conformance_version`; `TemplateBuilder._building` | `DELETE /v1/templates/{name}` does not check for sandboxes on that template (`api.py:580-586`), so a `created` or `paused_cold` sandbox can no longer start. Template GC checks "in use" and deletes the image in separate steps with no lock (`template_builder.py:422-443`). | sure / likely |
| H9 | Build contexts | `sha256-*.tar.gz`, `.partial` files | Sound in one process: the save is synchronous and the rename is atomic. Nothing deletes old contexts. | — |
| H10 | Execution notifier | `notify.py` waiter dictionaries, Postgres `LISTEN` channels | Sound. Every wait has a timeout and rereads the row, so a lost notification costs one poll tick. | — |
| H11 | OpenSandbox compatibility routes | `metadata` expiry keys | Delete, pause and resume call the /v1 handlers, so they make the same checks. Renew has no status check. See H5 for the lost renew. | likely |

## What Toolspoke relies on

These answers come from the code at `2cb1d87`.

- **A Toolspoke tool call that times out does not cancel the remote process.** Toolspoke stops
  polling and sends nothing. Harborbox has no way to stop a running execution:
  `POST /v1/executions/{id}/cancel` answers 409 "running execution interruption is not
  implemented" for anything past `queued` (`api.py:1392-1396`). The process runs until the
  `timeout_seconds` Toolspoke sent with it. Harborbox passes that value to OpenSandbox's execd,
  which terminates the command when it is reached (`RunCommandOpts.timeout`), and Harborbox
  stops waiting after a further `execution_deadline_grace_seconds`
  (`opensandbox_runtime.py:416-436`). A Toolspoke deadline shorter than `timeout_seconds` (the
  120 s queue timeout of a one-shot toolkit call) leaves the process running after Toolspoke
  has given up. A still-`queued` execution can be cancelled, but Toolspoke does not ask.
- **After the idle timeout the sandbox is paused cold, not killed.** The idle sweep snapshots the
  filesystem and removes the container (`opensandbox_runtime.py:575-595`). The next execution
  restores it from the snapshot, so files survive and processes do not. Three things do kill it:
  `max_sandbox_lifetime_seconds` (one day from creation), 30 days in `paused_cold`, and H2 above.
- **Toolspoke can ask whether an execution finished.** `GET /v1/executions/{id}` returns the
  status, exit code, stdout and stderr until the reaper purges finished executions after
  `reaper_killed_retention_days` (30). It needs the id from the `POST` response. There is no
  list of a sandbox's executions, so an id lost with a timed-out `POST` cannot be found again.
- **Sandbox create is not idempotent.** The id is generated by the server (`api.py:647`) and the
  request takes no idempotency key. A create that timed out on the client is safe to retry: a
  `created` row has no container and reserves nothing, and the reaper removes it after
  `reaper_stuck_created_after_seconds` (900). A caller that wants to find the orphan can put its
  own key in `metadata` and scan `GET /v1/sandboxes`, which has no filter.

## Method for each machine

1. Write a TLA+ model of the machine and its concurrent actors. Each rule the code relies on
   becomes an INVARIANT. A read and a later write in the code are two actions in the model.
2. Run pstack's `statechart` checker. It draws the chart and gives the shortest path to each
   broken rule.
3. Replay each path against the real code as a failing test, one step for each action. If it
   does not fail, the model is wrong.
4. Fix the code, rerun the model with the fix, and keep the test.
