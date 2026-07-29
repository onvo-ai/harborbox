# Harborbox

Harborbox is a self-hosted Python sandbox platform with an E2B-like developer
experience and a resource-aware parallel scheduler. It starts isolated Docker
containers on demand, executes stateful Python through IPython kernels, and
queues work only when admitting another sandbox would cross the configured
memory or CPU budget.

This is an initial single-node implementation intended for local, internal, and
single-tenant workloads. Ordinary Docker containers share the host kernel. For
hostile public multi-tenancy, run the sandbox image with a stronger runtime such
as gVisor on Linux before treating the isolation boundary as production-grade.

## What is included

- Durable PostgreSQL execution queue
- Parallel weighted admission based on hard memory reservations
- Live available-memory emergency guard
- FIFO scheduling with bounded backfilling and aging
- Stateful Python execution with stdout, stderr, rich results, and tracebacks
- Queued shell commands
- File read, write, list, and remove operations
- Hard memory, CPU, PID, tmpfs, output, payload, and timeout limits
- Warm pause, cold pause, resume, kill, and idle cold suspension
- Sync Python SDK
- E2B-shaped TypeScript SDK
- Streaming binary uploads with exact `/workspace` and `/tmp` paths
- Optional outbound sandbox network and alternate OCI runtime
- OpenAPI documentation at `/docs`

## Quick start

Create a local configuration:

```bash
cp .env.example .env
```

Replace both secrets in `.env`. On OrbStack, also set `DOCKER_SOCKET` to the
socket reported by:

```bash
docker context inspect --format '{{.Endpoints.docker.Host}}'
```

Remove the `unix://` prefix. Then build and start:

```bash
docker compose up --build
```

The health endpoint is available at `http://localhost:8000/health` and API docs
at `http://localhost:8000/docs`.

## Onvo Lite profile

The Onvo profile builds a second sandbox image containing DuckDB, pandas,
NumPy, the supported database drivers, Excel support, and Google Sheets
dependencies:

```bash
docker compose -f compose.yaml -f compose.onvo.yaml up --build
```

It also enables a non-internal Docker network for database and Google API
access, a 20-minute idle lifetime, and two concurrent shell executions in one
project sandbox. The default sandbox reservation is 1 GiB so it remains usable
on a 4 GiB local Docker VM. On a larger production host, set
`HARBORBOX_ONVO_SANDBOX_MEMORY_MB=2048` and raise concurrency only after load
testing.

The egress network permits unrestricted outbound connections by itself. Apply
host firewall rules or an egress gateway when destination-level restrictions
are required.

The API joins the explicitly named `harborbox-control` network with the alias
`harborbox-api`. A separately composed Onvo container can join that network as
an external network and use `HARBORBOX_BASE_URL=http://harborbox-api:8000`;
the host port remains bound to loopback.

## Python SDK

Install the project locally:

```bash
uv sync
```

Then:

```python
from harborbox_sdk import SandboxClient

client = SandboxClient(
    "http://localhost:8000",
    api_key="your-api-key",
)

with client.sandboxes.create(memory_mb=512, cpu=1) as sandbox:
    result = sandbox.run_code(
        """
x = 40
print("calculating")
x + 2
"""
    )
    print(result.text)
    print(result.logs.stdout)

    command = sandbox.commands.run("python --version")
    print(command.logs.stdout)

    sandbox.files.write("hello.txt", "persistent workspace")
    sandbox.files.write_bytes("/tmp/input.bin", b"\x00\x01")
```

Submit without blocking:

```python
job = sandbox.run_code("sum(range(10_000_000))", wait=False)
print(job.status, job.queue_position)
result = job.wait(timeout=300)
```

## TypeScript SDK

Build the server-side SDK:

```bash
cd sdk/typescript
npm install
npm run build
```

It implements the E2B-shaped surface used by Onvo Lite:

```typescript
import { Sandbox } from "@harborbox/sdk";

const sandbox = await Sandbox.create("onvo-data-processor", {
  timeoutMs: 20 * 60_000,
});

try {
  await sandbox.files.write("/tmp/data.csv", csvArrayBuffer);
  const result = await sandbox.commands.run(
    "python /tmp/transform.py",
    { timeoutMs: 8 * 60_000 },
  );
  console.log(result.stdout, result.stderr, result.exitCode);
} finally {
  await sandbox.kill();
}
```

Set `HARBORBOX_BASE_URL` and `HARBORBOX_API_KEY` in the server process. The
client automatically warms or resumes a lazy sandbox before file operations.

## Scheduling and memory safety

Every sandbox has a hard Docker memory limit. The scheduler reserves that entire
limit rather than assuming current low usage will continue. A new sandbox is
admitted only when both conditions hold:

```text
reserved sandbox memory + requested memory <= sandbox budget
host available memory - requested memory >= emergency reserve
```

The sandbox budget is:

```text
Docker host memory - host reserve - platform reserve
```

For example, an 8 GiB sandbox budget can run eight 1 GiB sandboxes, four 2 GiB
sandboxes, or any safe combination. Different sandboxes run concurrently.
Stateful kernel executions remain exclusive within a sandbox. Shell commands
may overlap up to `HARBORBOX_MAX_CONCURRENT_EXECUTIONS_PER_SANDBOX`; the
container's hard memory and CPU limits still bound their combined usage.

The `/v1/capacity` endpoint reports current reservations and queue counts.

### Pause behavior

- `sandbox.pause(memory=True)` uses Docker pause. Kernel variables and processes
  survive, but its full memory remains reserved.
- `sandbox.pause(memory=False)` stops the container. The workspace survives and
  memory is released, but kernel variables and processes do not.
- Idle sandboxes cold-pause after `idle_timeout_seconds` unless the value is `0`.

## Parent-machine protection

Harborbox constrains its own containers, but no application can prevent
unrelated host processes from exhausting the machine. Leave meaningful memory
headroom in both Harborbox and the Docker VM.

On Docker Desktop or OrbStack, cap the Linux VM itself below the Mac's physical
memory. A reasonable starting point is 50-65% of physical RAM. Harborbox then
reserves an additional 25% of the VM, with a 1 GiB minimum by default.

Important settings are documented in `.env.example`.

For a stronger isolation boundary, install gVisor on Linux and set
`HARBORBOX_SANDBOX_RUNTIME=runsc`. Harborbox passes the configured runtime to
Docker without assuming it is installed.

## REST API

Core endpoints:

```text
POST   /v1/sandboxes
GET    /v1/sandboxes
GET    /v1/sandboxes/{id}
PATCH  /v1/sandboxes/{id}
POST   /v1/sandboxes/{id}/pause
POST   /v1/sandboxes/{id}/resume
DELETE /v1/sandboxes/{id}

POST   /v1/sandboxes/{id}/executions
POST   /v1/sandboxes/{id}/commands
GET    /v1/executions/{id}
GET    /v1/executions/{id}/events
POST   /v1/executions/{id}/cancel

GET    /v1/sandboxes/{id}/files
PUT    /v1/sandboxes/{id}/files
PUT    /v1/sandboxes/{id}/files/content
GET    /v1/sandboxes/{id}/files/list
DELETE /v1/sandboxes/{id}/files

GET    /v1/capacity
```

Except for `/health`, requests require:

```text
X-API-Key: <HARBORBOX_API_KEY>
```

`PUT .../files/content?path=/tmp/file.bin` accepts
`application/octet-stream` and streams the request through the control plane.
It does not create a base64 copy of large uploads. File API absolute paths are
limited to `/workspace` and `/tmp`; other absolute roots and traversal are
rejected.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
docker compose config
docker compose -f compose.yaml -f compose.onvo.yaml config
```
