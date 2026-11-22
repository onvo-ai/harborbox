---- MODULE H2 ----
\* Harborbox idle ladder against execution admission, for one running sandbox.
\* Actors: the reaper's idle stage (_cold_pause_idle_sandboxes and _suspend),
\* a caller that queues one command, the scheduler's admission pass, and the
\* execution's run task. FIXED = TRUE models the code after the fix: the idle
\* stage claims the sandbox as `pausing`, and admission locks the sandbox row.
EXTENDS TLC, FiniteSets

CONSTANT FIXED

VARIABLES
    sbx,        \* sandboxes.status
    container,  \* up | none: is there a container in OpenSandbox
    rowSnap,    \* snapshot id the row records: none | s1
    snaps,      \* snapshots that exist in OpenSandbox
    ex,         \* executions.status, or "none" before the caller queues it
    adm,        \* admission pass: idle | read
    admSeen,    \* sandbox status admission read
    admLock,    \* admission holds the sandbox row lock (fix only)
    sweep       \* idle stage: idle | planned | paused | done

vars == <<sbx, container, rowSnap, snaps, ex, adm, admSeen, admLock, sweep>>

Active == {"admitted", "starting", "running"}

Init ==
    /\ sbx = "running"
    /\ container = "up"
    /\ rowSnap = "none"
    /\ snaps = {}
    /\ ex = "none"
    /\ adm = "idle"
    /\ admSeen = "none"
    /\ admLock = FALSE
    /\ sweep = "idle"

\* The caller posts a command.
Enqueue ==
    /\ ex = "none"
    /\ ex' = "queued"
    /\ UNCHANGED <<sbx, container, rowSnap, snaps, adm, admSeen, admLock, sweep>>

\* Admission reads the queued execution and its sandbox. After the fix it locks the sandbox row.
AdmitRead ==
    /\ adm = "idle" /\ ex = "queued"
    /\ adm' = "read" /\ admSeen' = sbx /\ admLock' = FIXED
    /\ UNCHANGED <<sbx, container, rowSnap, snaps, ex, sweep>>

\* Admission commits. It moves a cold sandbox to `starting`. After the fix it defers a `pausing` one.
AdmitCommit ==
    /\ adm = "read"
    /\ adm' = "idle" /\ admLock' = FALSE
    /\ IF FIXED /\ admSeen = "pausing"
       THEN UNCHANGED <<ex, sbx>>
       ELSE IF admSeen = "failed"
       THEN ex' = "failed" /\ UNCHANGED sbx
       ELSE /\ ex' = "admitted"
            /\ sbx' = IF admSeen = "paused_cold" THEN "starting" ELSE sbx
    /\ UNCHANGED <<container, rowSnap, snaps, admSeen, sweep>>

\* The run task marks it starting, brings the sandbox up, and sends the command.
RunStart ==
    /\ ex = "admitted"
    /\ CASE sbx = "failed" ->
              ex' = "failed" /\ UNCHANGED <<sbx, container>>
         [] sbx = "starting" /\ (rowSnap = "none" \/ rowSnap \in snaps) ->
              ex' = "running" /\ sbx' = "running" /\ container' = "up"
         [] sbx = "starting" ->
              ex' = "failed" /\ sbx' = "failed" /\ UNCHANGED container
         [] container = "up" ->
              ex' = "running" /\ UNCHANGED <<sbx, container>>
         [] OTHER ->
              ex' = "failed" /\ sbx' = "failed" /\ UNCHANGED container
    /\ UNCHANGED <<rowSnap, snaps, adm, admSeen, admLock, sweep>>

\* The command returns. If its container was removed, _fail_execution marks the sandbox failed.
CommandReturns ==
    /\ ex = "running"
    /\ IF container = "up"
       THEN ex' = "succeeded" /\ UNCHANGED sbx
       ELSE ex' = "failed" /\ sbx' = "failed"
    /\ UNCHANGED <<container, rowSnap, snaps, adm, admSeen, admLock, sweep>>

\* The idle stage picks the sandbox: running, idle, no active execution.
\* After the fix this is one conditional write to `pausing`, under the sandbox row lock.
SweepPlan ==
    /\ sweep = "idle" /\ sbx = "running" /\ ex \notin Active
    /\ IF FIXED
       THEN ~admLock /\ sbx' = "pausing"
       ELSE UNCHANGED sbx
    /\ sweep' = "planned"
    /\ UNCHANGED <<container, rowSnap, snaps, ex, adm, admSeen, admLock>>

\* runtime.pause(memory=False): take a snapshot, then remove the container.
SweepPause ==
    /\ sweep = "planned" /\ container = "up"
    /\ snaps' = snaps \cup {"s1"} /\ container' = "none" /\ sweep' = "paused"
    /\ UNCHANGED <<sbx, rowSnap, ex, adm, admSeen, admLock>>

\* _suspend records the tier. Before the fix it writes over `running` and skips anything else.
\* After the fix it writes only over its own `pausing` claim and deletes the snapshot otherwise.
SweepWrite ==
    /\ sweep = "paused"
    /\ sweep' = "done"
    /\ IF (FIXED /\ sbx = "pausing") \/ (~FIXED /\ sbx \in {"running", "paused_memory"})
       THEN sbx' = "paused_cold" /\ rowSnap' = "s1" /\ UNCHANGED snaps
       ELSE /\ UNCHANGED <<sbx, rowSnap>>
            /\ snaps' = IF FIXED THEN snaps \ {"s1"} ELSE snaps
    /\ UNCHANGED <<container, ex, adm, admSeen, admLock>>

Done ==
    /\ ex \in {"succeeded", "failed"} /\ sweep \in {"idle", "done"} /\ adm = "idle"
    /\ UNCHANGED vars

Next ==
    \/ Enqueue \/ AdmitRead \/ AdmitCommit \/ RunStart \/ CommandReturns
    \/ SweepPlan \/ SweepPause \/ SweepWrite
    \/ Done

Spec == Init /\ [][Next]_vars

\* The idle stage never removes a container while a command runs in it.
NoPauseUnderACommand == ex = "running" => container = "up"

\* Going idle never costs the caller the sandbox.
IdleNeverLosesTheSandbox == sbx # "failed"

\* Once the sweep has finished with it, every snapshot that exists is one the row points at.
NoOrphanSnapshot == sweep # "paused" => snaps \subseteq {rowSnap}

====
