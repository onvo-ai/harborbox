---- MODULE H3 ----
\* Harborbox reaper (reaper.py reap_once) against a sandbox coming back from paused_cold.
\* Actors: admission and the start it begins, a start that runs past its budget and is
\* retried, time passing, and one reaper sweep (read, then kill, then write).
\* The sandbox was idle past its timeout, so its last activity is already old.
\* FIXED = TRUE models the code after the fix: a stuck start is timed from when the
\* row entered `starting`, and the sweep claims `killed` conditionally before it kills.
EXTENDS TLC, Naturals

CONSTANT FIXED

VARIABLES
    sbx,          \* sandboxes.status
    ver,          \* stands for updated_at: bumped whenever the row changes
    snap,         \* the row's snapshot still exists in OpenSandbox
    container,    \* a container is up for this sandbox
    start,        \* the start: none | inflight | timedout | done
    activityOld,  \* last_activity_at is older than the stuck threshold
    startingOld,  \* the row has been `starting`, with no start begun, past the threshold
    rp,           \* reaper sweep: idle | read | claimed | killed | done
    rpStatus,     \* status the sweep read
    rpVer,        \* updated_at the sweep read
    reapedLive    \* the sweep destroyed a sandbox whose start was live, or that was running

vars == <<sbx, ver, snap, container, start, activityOld, startingOld, rp, rpStatus, rpVer, reapedLive>>

Init ==
    /\ sbx = "paused_cold" /\ ver = 0 /\ snap = TRUE /\ container = FALSE
    /\ start = "none" /\ activityOld = TRUE /\ startingOld = FALSE
    /\ rp = "idle" /\ rpStatus = "none" /\ rpVer = 0 /\ reapedLive = FALSE

\* A command is admitted: the row moves to `starting` and a start begins.
Admit ==
    /\ sbx = "paused_cold" /\ start = "none"
    /\ sbx' = "starting" /\ ver' = ver + 1 /\ start' = "inflight" /\ startingOld' = FALSE
    /\ UNCHANGED <<snap, container, activityOld, rp, rpStatus, rpVer, reapedLive>>

\* The start runs past sandbox_start_timeout_seconds. The row stays `starting`, retryable.
StartTimesOut ==
    /\ start = "inflight"
    /\ start' = "timedout"
    /\ UNCHANGED <<sbx, ver, snap, container, activityOld, startingOld, rp, rpStatus, rpVer, reapedLive>>

\* Nobody retries for longer than reaper_stuck_starting_after_seconds.
TimePasses ==
    /\ start = "timedout" /\ sbx = "starting" /\ ~startingOld
    /\ startingOld' = TRUE
    /\ UNCHANGED <<sbx, ver, snap, container, start, activityOld, rp, rpStatus, rpVer, reapedLive>>

\* The caller retries. After the fix, beginning a start stamps the row.
Retry ==
    /\ start = "timedout" /\ sbx = "starting"
    /\ start' = "inflight"
    /\ startingOld' = IF FIXED THEN FALSE ELSE startingOld
    /\ ver' = IF FIXED THEN ver + 1 ELSE ver
    /\ UNCHANGED <<sbx, snap, container, activityOld, rp, rpStatus, rpVer, reapedLive>>

\* The start returns (_ensure_running_locked's second session).
StartFinishes ==
    /\ start = "inflight"
    /\ start' = "done"
    /\ CASE ~snap ->
              \* OpenSandbox cannot restore a deleted snapshot; _mark_start_failed.
              /\ sbx' = IF sbx \in {"killed", "failed"} THEN sbx ELSE "failed"
              /\ ver' = ver + 1 /\ UNCHANGED <<container, activityOld>>
         [] sbx \in {"killed", "failed"} ->
              \* Killed while starting: the new container is removed again.
              UNCHANGED <<sbx, ver, container, activityOld>>
         [] OTHER ->
              /\ sbx' = "running" /\ ver' = ver + 1 /\ container' = TRUE
              /\ activityOld' = FALSE
    /\ UNCHANGED <<snap, startingOld, rp, rpStatus, rpVer, reapedLive>>

Stuck == IF FIXED THEN startingOld ELSE activityOld

\* The sweep reads the row and decides. Only a stuck `starting` row is a candidate here.
ReaperRead ==
    /\ rp = "idle"
    /\ rpStatus' = sbx /\ rpVer' = ver
    /\ rp' = IF sbx = "starting" /\ Stuck THEN "read" ELSE "done"
    /\ UNCHANGED <<sbx, ver, snap, container, start, activityOld, startingOld, reapedLive>>

Destroy ==
    /\ snap' = FALSE /\ container' = FALSE
    /\ reapedLive' = (reapedLive \/ start = "inflight" \/ sbx = "running")

\* Before the fix: runtime.kill with the row as read, then write `killed` over whatever is there.
ReaperKill ==
    /\ ~FIXED /\ rp = "read"
    /\ Destroy /\ rp' = "killed"
    /\ UNCHANGED <<sbx, ver, start, activityOld, startingOld, rpStatus, rpVer>>

ReaperWrite ==
    /\ ~FIXED /\ rp = "killed"
    /\ sbx' = "killed" /\ ver' = ver + 1 /\ rp' = "done"
    /\ UNCHANGED <<snap, container, start, activityOld, startingOld, rpStatus, rpVer, reapedLive>>

\* After the fix: UPDATE ... SET killed WHERE status and updated_at are as read, then kill.
ReaperClaim ==
    /\ FIXED /\ rp = "read"
    /\ IF sbx = rpStatus /\ ver = rpVer
       THEN sbx' = "killed" /\ ver' = ver + 1 /\ rp' = "claimed"
       ELSE UNCHANGED <<sbx, ver>> /\ rp' = "done"
    /\ UNCHANGED <<snap, container, start, activityOld, startingOld, rpStatus, rpVer, reapedLive>>

ReaperKillClaimed ==
    /\ FIXED /\ rp = "claimed"
    /\ Destroy /\ rp' = "done"
    /\ UNCHANGED <<sbx, ver, start, activityOld, startingOld, rpStatus, rpVer>>

Done ==
    /\ rp = "done" /\ start \in {"done", "timedout"}
    /\ UNCHANGED vars

Next ==
    \/ Admit \/ StartTimesOut \/ TimePasses \/ Retry \/ StartFinishes
    \/ ReaperRead \/ ReaperKill \/ ReaperWrite \/ ReaperClaim \/ ReaperKillClaimed
    \/ Done

Spec == Init /\ [][Next]_vars

\* Retries can loop forever; a few rounds show every interleaving that matters.
Bounded == ver <= 5

\* The sweep reclaims only starts nobody is waiting on: never a live start, never a running sandbox.
NoLiveSandboxReaped == ~reapedLive

\* No container runs for a row that says it is gone.
NoOrphanContainer == container => sbx \notin {"killed", "failed"}

\* Coming back from idle never fails because the sweep deleted the snapshot first.
ResumeNeverLosesTheSandbox == sbx # "failed"

====
