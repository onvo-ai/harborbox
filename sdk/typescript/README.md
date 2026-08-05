# Harborbox TypeScript SDK

An E2B-shaped client for server-side TypeScript applications. It implements
the subset used by Onvo Lite:

- `Sandbox.create(template, options?)` (template is mandatory)
- `sandbox.sandboxId`
- `sandbox.isRunning()`
- `sandbox.setTimeout(timeoutMs)`
- `sandbox.commands.run(command, options?)`
- `sandbox.files.write(path, string | ArrayBuffer)`
- `sandbox.kill()`

Configure `HARBORBOX_BASE_URL` and `HARBORBOX_API_KEY` in the server process.
The optional template name is retained as sandbox metadata; the Harborbox
deployment selects its sandbox image globally.
