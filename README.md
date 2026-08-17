# Harborbox

Harborbox is a durable admission and orchestration layer for OpenSandbox. It
owns the PostgreSQL execution queue, aggregate host budgets, scheduling policy,
and product-facing lifecycle API. Upstream OpenSandbox owns container creation,
commands, files, networking, snapshots, and isolation runtimes.

The bundled Compose deployment is a single-host Docker configuration intended
for internal and trusted workloads. Ordinary Docker containers share the host
kernel. Use OpenSandbox's Kubernetes runtime with gVisor, Kata, or Firecracker
before treating it as an isolation boundary for hostile public workloads.

```text
client -> Harborbox API -> PostgreSQL queue -> scheduler -> OpenSandbox -> runtime
```

## What is included

- Durable PostgreSQL execution queue
- Mandatory dependency-baked templates built with Docker BuildKit
- Content-hashed derived templates so callers with identical requirements share one image
- PostgreSQL-coordinated adaptive warm pools using the official OpenSandbox SDK
- Parallel weighted admission based on hard memory reservations
- Live available-memory emergency guard
- FIFO scheduling with bounded backfilling and aging
- Upstream OpenSandbox lifecycle, execution, filesystem, and snapshot APIs
- Python execution as an ordinary command, with no kernel in the sandbox
- Queued shell commands
- Queued argv-safe process execution with per-call secret environments
- File read, write, list, and remove operations
- Hard OpenSandbox memory and CPU limits plus bounded Harborbox output and uploads
- Warm pause, cold pause, resume, kill, and idle cold suspension
- Sync Python SDK
- E2B-shaped TypeScript SDK
- Streaming binary uploads with exact `/workspace` and `/tmp` paths
- OpenSandbox egress, credential vault, and secure runtime compatibility
- OpenAPI documentation at `/docs`

## Quick start

Create a local configuration:

```bash
cp .env.example .env
```

Replace all secrets in `.env`. On OrbStack, also set `DOCKER_SOCKET` to the
socket reported by:

```bash
docker context inspect --format '{{.Endpoints.docker.Host}}'
```

Remove the `unix://` prefix. Then build the immutable runtime templates, build
the API, and start:

```bash
./scripts/build-templates.sh
docker compose build api
docker compose up -d
```

The health endpoint is available at `http://localhost:8000/health` and API docs
at `http://localhost:8000/docs`.

The sandbox image services are behind an inactive `build` profile. They are
image build targets, not runtime services. Normal startup runs PostgreSQL,
Harborbox and the pinned OpenSandbox server.

OpenSandbox receives the host Docker socket in the bundled single-host setup.
That grants the OpenSandbox service host-level Docker control. Harborbox's API
also receives the socket, because `POST /v1/templates` builds derived template
images itself; both services therefore hold host-level Docker control. Point
both at the same daemon, so an image the API builds is visible to the sandbox
runtime under either runtime provider. If you do not use derived templates,
remove the socket mount from the `api` service and set
`HARBORBOX_TEMPLATE_GC_ENABLED=false`; the template endpoints then fail with a
recorded build error instead of running. For a stricter deployment, run
OpenSandbox on a separate worker host or Kubernetes cluster and point the
`HARBORBOX_OPENSANDBOX_*` settings at it.

## Templates and warm starts

Every sandbox must specify one registered template. There is no generic image
fallback: this keeps dependency contents, resource sizing, and startup behavior
predictable. The bundled templates are declared in `templates/manifest.yaml`:

- `relaydeck`: lightweight CLI and stdio MCP execution
- `onvo-pro`: pandas and NumPy
- `onvo-lite`: database drivers and broader data tooling

Templates follow the same core model as E2B templates: Docker installs every
dependency during the build, and runtime requests only reference the resulting
versioned image. Build all templates with:

```bash
HARBORBOX_TEMPLATE_VERSION=2026.08.03 ./scripts/build-templates.sh
```

For a registry-backed release, set a full image prefix and push directly from
BuildKit:

```bash
HARBORBOX_TEMPLATE_IMAGE_PREFIX=ghcr.io/acme/harborbox-sandbox \
HARBORBOX_TEMPLATE_VERSION=git-abc123 \
./scripts/build-templates.sh --push
```

Use immutable versions in production and configure every Harborbox host with
the same version. Changing dependencies creates a new template version rather
than modifying running sandboxes.

OpenSandbox's official asynchronous pool keeps the configured number of those
templates ready. PostgreSQL provides distributed leases, pool leader fencing,
the durable execution queue, and warm-pool state in one database.
Harborbox claims an exact template/resource match; a custom memory or CPU size
uses the same prebuilt image but follows the direct creation path. Pooled
sandboxes are disposable and are never returned to the pool after use.

Defaults are two 256 MiB Relaydeck sandboxes, one 1 GiB Onvo Pro sandbox, and no
hot Onvo Lite sandbox. After five minutes without demand, idle pools scale to
zero. The next request uses the direct path while the pool refills in the
background. Warm-pool maximums remain part of admission headroom so a refill
cannot push the host over its aggregate memory or CPU cap.

## Derived templates

A tool that needs a binary in the sandbox — Chromium for Playwright, `psql` for
Postgres — should not force that binary on every tenant. `POST /v1/templates`
layers a caller-supplied requirement set onto one of the static bases above and
returns a content-hashed template:

```bash
curl -sX POST localhost:8000/v1/templates -H "X-API-Key: $HARBORBOX_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "base": "relaydeck",
    "apt": ["chromium", "fonts-liberation"],
    "npm": ["@playwright/mcp@0.0.78"],
    "env": {"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
  }'
```

The name is `<base>-<hash>`, where the hash is the first 12 hex characters of
the SHA-256 of the canonical spec — `base`, deduplicated and sorted `apt` and
`npm`, and key-sorted `env`. Callers with identical requirements therefore share
one image, so the image count is bounded by distinct requirement combinations
rather than by customer count. Resource overrides are deliberately excluded from
the hash, which makes the stored `memory_mb`/`cpu` a default hint only; pass
sizing explicitly on `POST /v1/sandboxes` when it matters per caller.

The call is idempotent: an identical spec returns `200` with the existing
template, a new one returns `201` and starts an asynchronous build, and a spec
whose previous build failed is retried. Poll `GET /v1/templates/{name}` for
`status` (`building`, `ready`, `failed`) and, on failure, a readable `error`
carrying the tail of the build log. An empty spec is not a template at all: it
returns the base unchanged and nothing is built.

Because the Dockerfile is generated from request input, package names are
treated as hostile. Each is rejected for shell metacharacters, checked against a
strict regex, and then checked against `HARBORBOX_TEMPLATE_APT_ALLOWLIST` or
`HARBORBOX_TEMPLATE_NPM_ALLOWLIST`; list lengths, environment variable count,
name shape and value size are all capped. The generated Dockerfile declares
`ENV` before the install layers — install steps read the environment, and
`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` only suppresses Playwright's browser download
if it is set before `npm install -g` runs — takes `USER root` for the installs
and restores `USER 10001:10001` at the end.

Derived templates cold-start by design. Warm pools stay configured for the
static templates only: pooling per requirement set would trade the bounded image
count for an unbounded pool count. A caller with no extra requirements stays on
its base template and keeps that pool.

Builds go through BuildKit, invoked as `docker build -` with the generated
Dockerfile on stdin and **no build context at all**, which is why the API image
ships the Docker CLI and the buildx plugin. Sending no context is deliberate: a
Dockerfile generated from API input cannot then `COPY` or `ADD` anything off the
build host. It is also the only option that works — Docker Engine 29 removed the
classic builder that the docker-py build API talks to, and calls to it hang
rather than fail.

Rebuilding a base image does not propagate to derived images until their spec
changes; `HARBORBOX_TEMPLATE_VERSION` is the lever for forcing that. Derived
images that no sandbox has used for `HARBORBOX_TEMPLATE_GC_MAX_IDLE_DAYS` are
removed by a sweep inside the existing reaper loop. `DELETE /v1/templates/{name}`
removes one immediately and refuses static names with `409`.

## Onvo Lite image

The Onvo Lite image contains DuckDB, pandas, NumPy, the supported database
drivers, Excel support, and Google Sheets dependencies. OpenSandbox provides
the execution daemon and network controls. Harborbox configures a
20-minute idle lifetime, and two concurrent shell executions in one project
sandbox. The default sandbox reservation is 1 GiB so it remains usable on a
4 GiB local Docker VM. On a larger production host, set
`HARBORBOX_ONVO_SANDBOX_MEMORY_MB=2048` and raise concurrency only after load
testing.

Configure destination-level restrictions and credential injection through
OpenSandbox's egress and Credential Vault facilities.

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

with client.sandboxes.create(
    template="onvo-pro",
    memory_mb=1024,
    cpu=2,
) as sandbox:
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

const sandbox = await Sandbox.create("onvo-lite", {
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

Every sandbox receives a hard OpenSandbox memory limit. The scheduler reserves
that entire limit rather than assuming current low usage will continue. A new sandbox is
admitted only when both conditions hold:

```text
reserved sandbox memory + requested memory <= sandbox budget
host available memory - requested memory >= emergency reserve
```

The effective sandbox budget is the smallest applicable ceiling derived from:

```text
HARBORBOX_SANDBOX_MEMORY_BUDGET_MB
host memory - host reserve - platform reserve
```

For example, an 8 GiB sandbox budget can run eight 1 GiB sandboxes, four 2 GiB
sandboxes, or any safe combination. Different sandboxes run concurrently.
Stateful kernel executions remain exclusive within a sandbox. Shell commands
may overlap up to `HARBORBOX_MAX_CONCURRENT_EXECUTIONS_PER_SANDBOX`; the
container's hard memory and CPU limits still bound their combined usage.

The `/v1/capacity` endpoint reports current reservations, warm-pool headroom,
and queue counts.

### Pause behavior

- `sandbox.pause(memory=True)` delegates to OpenSandbox pause. On Docker,
  processes survive and the full memory remains reserved. Resuming is an
  unfreeze, which is the only resume path that is plausibly sub-second.
- `sandbox.pause(memory=False)` creates an OpenSandbox filesystem snapshot and
  terminates the runtime. CPU and memory are released; files survive, live
  processes do not. Resuming builds a new container from the snapshot, which
  costs seconds.

Idle sandboxes walk down both tiers rather than dropping straight to cold:

```text
running --(hot_pause_idle_seconds)--> paused_memory --(idle_timeout_seconds)--> paused_cold
```

Freezing first keeps the container for a sandbox that is used again shortly
afterwards, while anything genuinely finished still goes cold on the same
`idle_timeout_seconds` as before. Because a frozen sandbox holds its whole
memory reservation, the tier is capped by `HARBORBOX_HOT_PAUSE_BUDGET_MB`;
past the cap, sandboxes go straight to cold. Set
`HARBORBOX_HOT_PAUSE_IDLE_SECONDS=0` to disable the tier entirely and restore
the previous behavior. `idle_timeout_seconds: 0` still means "never suspend".

## Parent-machine protection

Harborbox prevents its own queue from admitting reservations above the configured
budget, but no application can prevent unrelated host processes from exhausting
the machine. Leave meaningful memory headroom in OpenSandbox and the Docker VM.

On Docker Desktop or OrbStack, cap the Linux VM itself below the Mac's physical
memory. A reasonable starting point is 50-65% of physical RAM. Harborbox then
reserves an additional 25% of the VM, with a 1 GiB minimum by default.

Important settings are documented in `.env.example`.

For a stronger isolation boundary, configure OpenSandbox's `[secure_runtime]`
block for gVisor or Kata on Linux, or deploy its Kubernetes backend with an
appropriate RuntimeClass.

## REST API

Core endpoints:

```text
GET    /v1/templates
POST   /v1/templates
GET    /v1/templates/{name}
DELETE /v1/templates/{name}

POST   /v1/sandboxes
GET    /v1/sandboxes
GET    /v1/sandboxes/{id}
PATCH  /v1/sandboxes/{id}
POST   /v1/sandboxes/{id}/pause
POST   /v1/sandboxes/{id}/resume
DELETE /v1/sandboxes/{id}

POST   /v1/sandboxes/{id}/executions
POST   /v1/sandboxes/{id}/commands
POST   /v1/sandboxes/{id}/processes
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

`POST /v1/sandboxes` requires `template` to name either a statically configured
template — `relaydeck`, `onvo-pro`, `onvo-lite` — or a derived template
registered through `POST /v1/templates`. An unknown name is a `422`; a known
name whose image is still building or has failed is a `409` carrying the build
status.

`GET /v1/templates` returns `{"templates": [...]}` covering both the static and
the derived registry. Static entries have a null `spec_hash` and a `status` of
`ready`.

`POST .../executions`, `.../commands` and `.../processes` accept
`"wait": true`, which holds the connection until the execution finishes and
answers `200` with the result instead of `202` with a job id. If the execution
outlives the wait the response is still `202`, so a client that asked to wait
and ran out of patience simply polls as before. Both SDKs send it by default.

`PUT .../files/content?path=/tmp/file.bin` accepts
`application/octet-stream` and streams the request through the control plane.
It does not create a base64 copy of large uploads. File API absolute paths are
limited to `/workspace` and `/tmp`; other absolute roots and traversal are
rejected.

Python code runs as an ordinary command through `/opt/coderun.py`, which
reproduces the one thing a Jupyter kernel provided that a plain script does
not: the value of a trailing expression, returned in `results[0].text`. There
is no persistent interpreter, so the old special case for per-call environment
variables is gone -- every execution is a fresh forked child and a secret has
nowhere to linger. Where `/opt/forkrun.py` is present (onvo-pro, onvo-lite) the
child is forked from a daemon that has already imported pandas, which keeps the
~1.5 s import off every call.

Removing the kernel took ~3 s of boot and ~197 MB of resident memory out of
every onvo-pro and onvo-lite sandbox; see `scripts/bench/` for the harness and
`scripts/bench/results/` for the measurements.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
docker compose config
```
