export const title = "Harborbox sandbox start against DELETE";
const words = { starting: "Starting", running: "Running", killed: "Deleted", failed: "Failed" };
export const state = (v) => ({
  Row: words[v.sbx] + (v.rowCtr === "c" ? ", knows its container" : ""),
  Container: v.container === "up" ? "Container up" : "No container",
});
export const group = (v) => (v.container === "up" && ["killed", "failed"].includes(v.sbx) ? "orphan" : "ok");
export const colors = { orphan: "pink", ok: "blue" };
export const step = (action) => ({
  StartCreate: "OpenSandbox creates the container",
  StartRead: "the start reads the row",
  StartWrite: "the start saves running",
  StartWriteIfLive: "the start saves running",
  DelRead: "you delete the sandbox",
  DelKill: "DELETE kills what it knows of",
  DelWrite: "DELETE saves killed",
  Done: null,
}[action] ?? action);
