---- MODULE H4 ----
\* Harborbox execution lifecycle: one execution on one sandbox.
\* Actors: the scheduler's admission pass, the execution's run task,
\* DELETE /v1/sandboxes/{id}, and POST /v1/executions/{id}/cancel.
\* FIXED = TRUE models the code after the fix.
EXTENDS TLC

CONSTANT FIXED

VARIABLES
    ex,         \* executions.status
    cancelReq,  \* executions.cancel_requested
    sbx,        \* sandboxes.status
    adm,        \* admission pass: idle | locked | done
    admSbx,     \* sandbox status admission read (no row lock on the sandbox)
    run,        \* run task: none | spawned | starting | ensured | cmd | result | done
    del,        \* DELETE request: idle | cancelled | done
    can,        \* cancel request: idle | read | done
    canRead,    \* status the cancel request read
    answer,     \* what the cancel request told the caller
    ran,        \* the command ran in the container
    final       \* the first terminal status the execution reached

vars == <<ex, cancelReq, sbx, adm, admSbx, run, del, can, canRead, answer, ran, final>>

Active == {"admitted", "starting", "running"}
Terminal == {"succeeded", "failed", "cancelled"}

SetEx(s) ==
    /\ ex' = s
    /\ final' = IF final = "none" /\ s \in Terminal THEN s ELSE final

Init ==
    /\ ex = "queued"
    /\ cancelReq = FALSE
    /\ sbx = "created"
    /\ adm = "idle"
    /\ admSbx = "none"
    /\ run = "none"
    /\ del = "idle"
    /\ can = "idle"
    /\ canRead = "none"
    /\ answer = "none"
    /\ ran = FALSE
    /\ final = "none"

\* Admission: SELECT ... FOR UPDATE OF executions. The sandbox row is read, not locked.
AdmitLock ==
    /\ adm = "idle" /\ ex = "queued"
    /\ adm' = "locked" /\ admSbx' = sbx
    /\ UNCHANGED <<ex, cancelReq, sbx, run, del, can, canRead, answer, ran, final>>

\* Admission commits: reject (_reject_ineligible) or admit (_admit_or_defer).
AdmitCommit ==
    /\ adm = "locked"
    /\ adm' = "done"
    /\ IF cancelReq
       THEN SetEx("cancelled") /\ UNCHANGED <<sbx, run>>
       ELSE IF admSbx = "killed"
       THEN SetEx("failed") /\ UNCHANGED <<sbx, run>>
       ELSE /\ SetEx("admitted")
            /\ run' = "spawned"
            /\ sbx' = IF admSbx = "created" /\ sbx = "created" THEN "starting" ELSE sbx
    /\ UNCHANGED <<cancelReq, admSbx, del, can, canRead, answer, ran>>

\* _mark_starting: re-validate, then mark starting. Before the fix a refusal wrote nothing.
MarkStarting ==
    /\ run = "spawned"
    /\ IF ~cancelReq /\ ex \notin Terminal /\ sbx # "killed"
       THEN /\ SetEx("starting") /\ run' = "starting"
       ELSE /\ run' = "done"
            /\ IF FIXED /\ ex = "admitted"
               THEN SetEx(IF cancelReq THEN "cancelled" ELSE "failed")
               ELSE UNCHANGED <<ex, final>>
    /\ UNCHANGED <<cancelReq, sbx, adm, admSbx, del, can, canRead, answer, ran>>

\* _ensure_running: start the container, or fail the execution if the sandbox is gone.
EnsureRunning ==
    /\ run = "starting"
    /\ IF sbx = "killed"
       THEN /\ run' = "done"
            /\ IF ex \in Terminal THEN UNCHANGED <<ex, final>> ELSE SetEx("failed")
            /\ UNCHANGED sbx
       ELSE /\ run' = "ensured" /\ sbx' = "running" /\ UNCHANGED <<ex, final>>
    /\ UNCHANGED <<cancelReq, adm, admSbx, del, can, canRead, answer, ran>>

\* _mark_running: writes `running` with no status check.
MarkRunning ==
    /\ run = "ensured"
    /\ SetEx("running") /\ run' = "cmd"
    /\ UNCHANGED <<cancelReq, sbx, adm, admSbx, del, can, canRead, answer, ran>>

\* runtime.execute_*: the command runs, unless the container was killed.
Execute ==
    /\ run = "cmd"
    /\ IF sbx = "killed"
       THEN /\ run' = "done"
            /\ IF ex \in Terminal THEN UNCHANGED <<ex, final>> ELSE SetEx("failed")
            /\ UNCHANGED ran
       ELSE /\ run' = "result" /\ ran' = TRUE /\ UNCHANGED <<ex, final>>
    /\ UNCHANGED <<cancelReq, sbx, adm, admSbx, del, can, canRead, answer>>

\* _record_result: writes the outcome with no status check.
RecordResult ==
    /\ run = "result"
    /\ SetEx("succeeded") /\ run' = "done"
    /\ UNCHANGED <<cancelReq, sbx, adm, admSbx, del, can, canRead, answer, ran>>

\* DELETE: UPDATE executions SET cancelled WHERE status = 'queued'. Waits on admission's lock.
DeleteCancelQueued ==
    /\ del = "idle" /\ adm # "locked"
    /\ del' = "cancelled"
    /\ IF ex = "queued"
       THEN SetEx("cancelled") /\ cancelReq' = TRUE
       ELSE UNCHANGED <<ex, final, cancelReq>>
    /\ UNCHANGED <<sbx, adm, admSbx, run, can, canRead, answer, ran>>

\* DELETE: runtime.kill, then sandboxes.status = 'killed'.
DeleteKill ==
    /\ del = "cancelled"
    /\ del' = "done" /\ sbx' = "killed"
    /\ UNCHANGED <<ex, cancelReq, adm, admSbx, run, can, canRead, answer, ran, final>>

\* Cancel: read the execution.
CancelRead ==
    /\ can = "idle"
    /\ can' = "read" /\ canRead' = ex
    /\ UNCHANGED <<ex, cancelReq, sbx, adm, admSbx, run, del, answer, ran, final>>

\* Cancel: write. Before the fix the write has no status check; after it, WHERE status = 'queued'.
CancelWrite ==
    /\ can = "read" /\ adm # "locked"
    /\ can' = "done"
    /\ IF FIXED
       THEN IF ex = "queued"
            THEN SetEx("cancelled") /\ cancelReq' = TRUE /\ answer' = "cancelled"
            ELSE UNCHANGED <<ex, final, cancelReq>>
                 /\ answer' = IF ex \in Active THEN "refused" ELSE ex
       ELSE IF canRead = "queued"
            THEN SetEx("cancelled") /\ cancelReq' = TRUE /\ answer' = "cancelled"
            ELSE UNCHANGED <<ex, final, cancelReq>>
                 /\ answer' = IF canRead \in Active THEN "refused" ELSE canRead
    /\ UNCHANGED <<sbx, adm, admSbx, run, del, canRead, ran>>

Done ==
    /\ adm # "locked" /\ run \in {"none", "done"} /\ del # "cancelled" /\ can # "read"
    /\ UNCHANGED vars

Next ==
    \/ AdmitLock \/ AdmitCommit
    \/ MarkStarting \/ EnsureRunning \/ MarkRunning \/ Execute \/ RecordResult
    \/ DeleteCancelQueued \/ DeleteKill
    \/ CancelRead \/ CancelWrite
    \/ Done

Spec == Init /\ [][Next]_vars

\* Once its run task has ended, an execution is finished: no one else will finish it.
NoStrandedExecution == (run = "done") => ex \notin Active

\* A finished execution stays finished, with the outcome it first had.
TerminalIsFinal == final # "none" => ex = final

\* A cancel that answered "cancelled" stopped the command from running.
CancelTellsTruth == answer = "cancelled" => ~ran

\* A sandbox stays `starting` only while a start for it is still coming.
StartingHasAStart == sbx = "starting" => run \in {"spawned", "starting"}

====
