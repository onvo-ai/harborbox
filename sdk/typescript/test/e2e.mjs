import assert from "node:assert/strict";
import { Sandbox } from "../dist/index.js";

const sandbox = await Sandbox.create("onvo-data-processor", {
  memoryMb: 768,
  cpu: 1,
  timeoutMs: 120_000,
});

try {
  const payload = new TextEncoder().encode("typescript-binary-upload");
  await sandbox.files.write("/tmp/typescript.bin", payload);
  const read = await sandbox.commands.run(
    "python -c \"from pathlib import Path; print(Path('/tmp/typescript.bin').read_text())\"",
  );
  assert.equal(read.exitCode, 0);
  assert.match(read.stdout, /typescript-binary-upload/);

  await sandbox.setTimeout(180_000);
  assert.equal(await sandbox.isRunning(), true);

  const startedAt = Date.now();
  const [first, second] = await Promise.all([
    sandbox.commands.run("sleep 2; echo first", { timeoutMs: 10_000 }),
    sandbox.commands.run("sleep 2; echo second", { timeoutMs: 10_000 }),
  ]);
  const elapsedMs = Date.now() - startedAt;
  assert.equal(first.exitCode, 0);
  assert.equal(second.exitCode, 0);
  assert.ok(elapsedMs < 3_800, `commands did not overlap: ${elapsedMs}ms`);

  console.log({
    typescript_sdk: "ok",
    absolute_tmp_binary: "ok",
    same_sandbox_parallel_ms: elapsedMs,
  });
} finally {
  await sandbox.kill();
}
