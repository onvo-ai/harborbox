// Unit tests for the SDK. They stub `fetch`, so they need no running server.
// Run with `npm test` (builds first). The live-server checks are in e2e.mjs.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { Sandbox } from "../dist/index.js";

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

function json(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** Run one command against a stub server whose execution has these logs. */
async function runWithLogs(logs) {
  globalThis.fetch = async (url, init = {}) => {
    const { pathname } = new URL(url);
    if (init.method === "POST" && pathname === "/v1/sandboxes") {
      return json({
        id: "sbx-1",
        status: "running",
        memory_mb: 512,
        cpu: 1,
        idle_timeout_seconds: 60,
        metadata: {},
      });
    }
    if (init.method === "POST" && pathname === "/v1/sandboxes/sbx-1/commands") {
      return json({
        id: "exe-1",
        status: "succeeded",
        logs: logs === null ? null : { truncated: false, ...logs },
        error: null,
        exit_code: 0,
      });
    }
    throw new Error(`unexpected request: ${init.method ?? "GET"} ${pathname}`);
  };
  const sandbox = await Sandbox.create("base", {
    baseUrl: "http://harborbox.test",
    apiKey: "unit-test-key",
  });
  return sandbox.commands.run("true");
}

test("multi-line stdout keeps its line breaks and blank lines", async () => {
  // printf 'alpha\n\nbeta\ngamma\n', in the API's per-line shape.
  const result = await runWithLogs({
    stdout: ["alpha", "\n", "beta", "gamma"],
    stderr: [],
  });
  assert.equal(result.stdout, "alpha\n\nbeta\ngamma\n");
  assert.deepEqual(result.stdout.split("\n"), [
    "alpha",
    "",
    "beta",
    "gamma",
    "",
  ]);
});

test("stderr is rebuilt the same way as stdout", async () => {
  const result = await runWithLogs({
    stdout: [],
    stderr: ["Traceback (most recent call last):", "\n", "ValueError: x"],
  });
  assert.equal(
    result.stderr,
    "Traceback (most recent call last):\n\nValueError: x\n",
  );
});

test("a single line ends with a newline, as E2B's echo output does", async () => {
  const result = await runWithLogs({ stdout: ["hi"], stderr: [] });
  assert.equal(result.stdout, "hi\n");
});

test("consecutive and trailing blank lines are kept", async () => {
  const result = await runWithLogs({
    stdout: ["\n", "\n", "x", "\n"],
    stderr: [],
  });
  assert.equal(result.stdout, "\n\nx\n\n");
});

test("no output is an empty string", async () => {
  const empty = await runWithLogs({ stdout: [], stderr: [] });
  assert.equal(empty.stdout, "");
  assert.equal(empty.stderr, "");
  const missing = await runWithLogs(null);
  assert.equal(missing.stdout, "");
  assert.equal(missing.stderr, "");
});
