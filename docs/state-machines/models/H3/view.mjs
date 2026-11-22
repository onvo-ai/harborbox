export const title = "Harborbox reaper against a sandbox coming back from idle";
const words = {
  paused_cold: "Paused cold", starting: "Starting", running: "Running",
  killed: "Killed", failed: "Failed (lost)",
};
const startWords = { none: "", inflight: ", start in flight", timedout: ", start timed out", done: "" };
export const state = (v) =>
  words[v.sbx] + startWords[v.start] + (v.startingOld ? ", stuck" : "") +
  (v.container && v.sbx === "killed" ? ", container orphaned" : "");
export const group = (v) => (["killed", "failed"].includes(v.sbx) ? "gone" : v.sbx === "running" ? "live" : "rest");
export const colors = { gone: "pink", live: "teal", rest: "blue" };
export const step = (action) => ({
  Admit: "you post a command",
  StartTimesOut: "the start runs past its budget",
  TimePasses: "five minutes pass with no retry",
  Retry: "you retry",
  StartFinishes: "the start returns",
  ReaperRead: "the reaper reads the sandbox",
  ReaperKill: "the reaper kills it in OpenSandbox",
  ReaperWrite: "the reaper marks it killed",
  ReaperClaim: "the reaper marks it killed",
  ReaperKillClaimed: "the reaper kills it in OpenSandbox",
  Done: null,
}[action] ?? action);
