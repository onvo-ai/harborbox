export const title = "Harborbox execution lifecycle";
const words = {
  queued: "Queued", admitted: "Admitted", starting: "Starting", running: "Running",
  succeeded: "Succeeded", failed: "Failed", cancelled: "Cancelled",
};
export const state = (v) => ({
  Execution: words[v.ex] + (v.run === "done" && ["admitted", "starting", "running"].includes(v.ex) ? " (no task left)" : ""),
  Sandbox: v.sbx === "killed" ? "Sandbox deleted" : v.sbx === "starting" ? "Sandbox starting" : v.sbx === "running" ? "Sandbox running" : "Sandbox created",
});
export const group = (v) => (["succeeded", "failed", "cancelled"].includes(v.ex) ? "done" : "live");
export const colors = { done: "teal", live: "yellow" };
export const step = (action) => ({
  AdmitLock: "the scheduler picks the execution",
  AdmitCommit: "the scheduler admits it",
  MarkStarting: "the run task begins",
  EnsureRunning: "the sandbox comes up",
  MarkRunning: "the command is sent",
  Execute: "the command returns",
  RecordResult: "the result is saved",
  DeleteCancelQueued: "you delete the sandbox",
  DeleteKill: "the container is removed",
  CancelRead: "you cancel the execution",
  CancelWrite: "the cancel is saved",
  Done: null,
}[action] ?? action);
