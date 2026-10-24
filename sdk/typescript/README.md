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

## Command output

`commands.run` returns `stdout` and `stderr` as text with one line per `"\n"`.
Every line ends with `"\n"`, including the last one. A command with no output
gives `""`. So `printf 'alpha\n\nbeta\n'` gives `"alpha\n\nbeta\n"`, as in E2B.

The API sends output as one array element per line, so it cannot tell whether
the last line ended with a newline. The SDK always adds one: `printf hi` gives
`"hi\n"` here, where E2B gives `"hi"`.

## Tests

```bash
npm install
npm test           # builds, then runs test/unit.mjs against a stubbed fetch
node test/e2e.mjs  # needs a live Harborbox: HARBORBOX_BASE_URL, HARBORBOX_API_KEY
```
