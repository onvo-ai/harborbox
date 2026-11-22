export const title = "Harborbox idle pause against a new command";
const sandbox = {
  running: "Running", pausing: "Pausing", paused_cold: "Paused cold",
  starting: "Starting", failed: "Failed (lost)",
};
const command = {
  none: "No command", queued: "Command queued", admitted: "Command admitted",
  running: "Command running", succeeded: "Command done", failed: "Command failed",
};
export const state = (v) => ({
  Sandbox: sandbox[v.sbx] + (v.container === "up" ? "" : ", no container"),
  Command: command[v.ex],
});
export const group = (v) => (v.sbx === "failed" ? "lost" : v.sbx === "running" ? "live" : "rest");
export const colors = { lost: "pink", live: "teal", rest: "blue" };
export const step = (action) => ({
  Enqueue: "you post a command",
  AdmitRead: "the scheduler reads the queue",
  AdmitCommit: "the scheduler admits the command",
  RunStart: "the command starts",
  CommandReturns: "the command returns",
  SweepPlan: "the idle sweep picks the sandbox",
  SweepPause: "the sandbox is snapshotted and its container removed",
  SweepWrite: "the sweep records the pause",
  Done: null,
}[action] ?? action);
