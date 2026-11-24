---- MODULE H1 ----
\* Harborbox sandbox start against DELETE /v1/sandboxes/{id}, for one sandbox in `starting`.
\* Actors: the start (_ensure_running_locked: create in OpenSandbox, then read the row,
\* then write it) and the DELETE request (read the row, kill in the runtime, write killed).
\* FIXED = TRUE models the code after the fix: DELETE commits `killed` before it kills,
\* and the start's write is conditional on the row not being killed.
EXTENDS TLC

CONSTANT FIXED

VARIABLES
    sbx,        \* sandboxes.status
    rowCtr,     \* sandboxes.container_id: none | c
    container,  \* the container in OpenSandbox: none | up
    cached,     \* the runtime's handle cache holds the new container
    st,         \* start: creating | created | read | done
    stRead,     \* status the start read
    del,        \* DELETE: idle | read | claimed | killedrt | done
    delCopy     \* container_id in DELETE's copy of the row

vars == <<sbx, rowCtr, container, cached, st, stRead, del, delCopy>>

Init ==
    /\ sbx = "starting" /\ rowCtr = "none" /\ container = "none" /\ cached = FALSE
    /\ st = "creating" /\ stRead = "none" /\ del = "idle" /\ delCopy = "none"

\* OpenSandbox creates the container; start_sandbox caches its handle.
StartCreate ==
    /\ st = "creating"
    /\ container' = "up" /\ cached' = TRUE /\ st' = "created"
    /\ UNCHANGED <<sbx, rowCtr, stRead, del, delCopy>>

\* Before the fix: the start reads the row, then writes it in a later step.
StartRead ==
    /\ ~FIXED /\ st = "created"
    /\ stRead' = sbx /\ st' = "read"
    /\ UNCHANGED <<sbx, rowCtr, container, cached, del, delCopy>>

StartWrite ==
    /\ ~FIXED /\ st = "read"
    /\ st' = "done"
    /\ IF stRead \in {"killed", "failed"}
       THEN container' = "none" /\ cached' = FALSE /\ UNCHANGED <<sbx, rowCtr>>
       ELSE sbx' = "running" /\ rowCtr' = "c" /\ UNCHANGED <<container, cached>>
    /\ UNCHANGED <<stRead, del, delCopy>>

\* After the fix: UPDATE ... SET running WHERE status NOT IN (killed, failed), in one step.
StartWriteIfLive ==
    /\ FIXED /\ st = "created"
    /\ st' = "done"
    /\ IF sbx \in {"killed", "failed"}
       THEN container' = "none" /\ cached' = FALSE /\ UNCHANGED <<sbx, rowCtr>>
       ELSE sbx' = "running" /\ rowCtr' = "c" /\ UNCHANGED <<container, cached>>
    /\ UNCHANGED <<stRead, del, delCopy>>

\* DELETE reads the row.
DelRead ==
    /\ del = "idle"
    /\ delCopy' = rowCtr /\ del' = IF FIXED THEN "claimed" ELSE "read"
    \* After the fix the read and the `killed` write are one committed step.
    /\ sbx' = IF FIXED THEN "killed" ELSE sbx
    /\ UNCHANGED <<rowCtr, container, cached, st, stRead>>

\* runtime.kill: the cached handle, or the container id DELETE holds.
\* After the fix DELETE rereads the row first, so it holds the latest container id.
DelKill ==
    /\ del \in {"read", "claimed"}
    /\ LET known == cached \/ (IF FIXED THEN rowCtr ELSE delCopy) = "c"
       IN /\ container' = IF known THEN "none" ELSE container
          /\ cached' = IF known THEN FALSE ELSE cached
    /\ del' = "killedrt"
    /\ UNCHANGED <<sbx, rowCtr, st, stRead, delCopy>>

\* DELETE writes killed and clears the container id.
DelWrite ==
    /\ del = "killedrt"
    /\ sbx' = "killed" /\ rowCtr' = "none" /\ del' = "done"
    /\ UNCHANGED <<container, cached, st, stRead, delCopy>>

Done ==
    /\ st = "done" /\ del = "done"
    /\ UNCHANGED vars

Next ==
    \/ StartCreate \/ StartRead \/ StartWrite \/ StartWriteIfLive
    \/ DelRead \/ DelKill \/ DelWrite
    \/ Done

Spec == Init /\ [][Next]_vars

\* Once both have finished, a deleted sandbox has no container left running.
NoOrphanContainer == (st = "done" /\ del = "done") => container = "none"

\* A sandbox the caller deleted stays deleted.
DeletedStaysDeleted == del = "done" => sbx \in {"killed", "failed"}

====
