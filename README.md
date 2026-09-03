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

An illustrated walkthrough of the same material -- the sandbox state machine,
an animated admission sweep, and a capacity calculator -- is published at
<https://onvo-ai.github.io/harborbox/>. It is generated from `site/index.html`
in this repository.

## What is included

- Durable PostgreSQL execution queue
- Templates are Dockerfiles the caller owns, built with rootless BuildKit
- Content-hashed templates, so identical Dockerfiles share one image
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
- E2B-shaped TypeScript SDK
- Streaming binary uploads with exact `/workspace` and `/tmp` paths
- OpenSandbox egress, credential vault, and secure runtime compatibility
- OpenAPI documentation at `/docs`

## Published images

Two images are published on every GitHub release, for `linux/amd64` and
`linux/arm64`, to GitHub Container Registry:

| Image | What it is |
| --- | --- |
| `ghcr.io/onvo-ai/harborbox-api` | The service. Speaks the HTTP API, owns the warm pool, and builds product templates at runtime. |
| `ghcr.io/onvo-ai/harborbox-sandbox-base` | The image sandboxes start `FROM`. Almost empty by design: uid/gid 10001, a writable `/workspace`, and CA certificates. |

Pin a version tag rather than tracking `latest` on anything you care about;
every image is also published under its git SHA if you want to pin exactly.

The base image is published rather than left to a local build because the
API's builder resolves a derived template's `FROM` over its own network and
cannot see the host daemon's image store. Point `TEMPLATE_REGISTRY` at a
registry both can reach, and `HARBORBOX_BASE_IMAGE` at the tag you pinned.

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

Remove the `unix://` prefix. The bundled registry hashes its own credentials
from `HARBORBOX_REGISTRY_USERNAME`/`HARBORBOX_REGISTRY_PASSWORD` at boot, so
there is nothing to create by hand for it.

Harborbox is **two Compose projects**: this one, and the rootless builder in
`compose.builder.yaml`. The split is a security boundary rather than a
packaging preference -- see "How template builds are isolated" below -- and it
means two things exist before either project starts, shared by both:

```bash
docker network create harborbox-build
./scripts/gen-buildkit-certs.sh
```

The first is the bridge the builder meets the registry on. The second issues
the mutual-TLS pair the API uses to drive buildkitd, into Docker volumes; no
key is ever written into the working tree. Then start the builder, build the
immutable runtime templates and push them to the registry, and start the rest:

```bash
docker compose -f compose.builder.yaml up -d
```

```bash
docker compose up -d registry
```

```bash
./scripts/build-templates.sh --push
```

```bash
docker compose build api && docker compose up -d
```

`./scripts/try-locally.sh` does all of the above and then proves the chain end
to end with a Dockerfile of its own; it is the fastest way to see this work.

The templates must be pushed, not merely built: the builder resolves each
derived template's `FROM` over its own network and cannot see the host daemon's
local image store.

The health endpoint is available at `http://localhost:8000/health` and API docs
at `http://localhost:8000/docs`.

The sandbox image services are behind an inactive `build` profile. They are
image build targets, not runtime services. Normal startup runs PostgreSQL,
Harborbox and the pinned OpenSandbox server.

### How template builds are isolated

OpenSandbox receives the host Docker socket in the bundled single-host setup,
which grants that service host-level Docker control. **Harborbox's API does
not.** It builds derived template images by driving a rootless BuildKit daemon
(`builder`) that holds no socket, so the control plane can ask for an image and
cannot start a container. A leaked `HARBORBOX_API_KEY` is therefore not by
itself arbitrary code execution on the host.

The thing to understand about that builder is that **a build step runs inside
buildkitd's network namespace**. There is no per-build network of its own: the
OCI worker's netmode is `host`, meaning buildkitd's own namespace, and the
rootless image ships no `slirp4netns` to give a step one. So every network the
builder container joins is a network that a caller's `RUN` can reach, and the
question "what can a hostile Dockerfile talk to" reduces to "what is on the
builder's networks".

Four properties follow, and each was verified against a live stack rather than
assumed — the evidence is in `docs/arbitrary-dockerfile-templates.md` section
10:

- **The builder is its own Compose project**, deployed separately, holding one
  service. This is not tidiness. Orchestrators append a per-application network
  to every service they deploy — Coolify does, unconditionally, and its
  `connect_to_docker_network` setting does not stop it — and while the builder
  was a service alongside the API, that appended network put `api:8000`,
  `opensandbox:8080` and `postgres:5432` within reach of every caller-supplied
  build step. Nothing inside a compose file can prevent the attachment. What it
  can do is leave nothing on the other end of it.
- **The API reaches buildkitd over authenticated TCP**, through the dual-homed
  `buildkit-gateway`, and never joins the build network itself. The unix socket
  this replaces was stronger and cannot cross projects. Because a build step
  can reach that gateway, buildkitd requires and verifies a client certificate
  (`[grpc.tls] ca`); `Settings` refuses to start a `tcp://` builder with no
  certificate configured, since an unauthenticated one would hand every caller
  a build daemon while every build kept working.
- **The registry has two addresses for one store.** BuildKit dials
  `registry:5000` over the build network; the reference Harborbox stores and
  hands to OpenSandbox is `127.0.0.1:5050`, because the *Docker daemon*
  resolves it from the host, where `registry` means nothing. Everything after
  the host part must stay identical, or the build succeeds and the sandbox
  create that follows fails on a missing image.
- **`network_mode: host` is never used for the builder.** Under it a build step
  reached a service published on host loopback, which on a single-host
  deployment means PostgreSQL and the API itself.

Whether that holds is measured, not asserted: `tests/e2e_build_isolation.py`
builds a template whose Dockerfile opens sockets to the control plane and reads
the results out of a sandbox made from the image. It requires `registry:5000`
to answer, so a broken probe fails rather than passes quietly.

```
reachable from inside a build step:
  registry:5000            REACHED
  api:8000                 unreachable
  harborbox-api:8000       unreachable
  postgres:5432            unreachable
  opensandbox:8080         unreachable
```

Leaving `HARBORBOX_BUILDER_ADDRESS` unset keeps the pre-registry behaviour:
builds go through a mounted Docker socket against the local daemon, and the API
holds host-level Docker control again. If you use neither, set
`HARBORBOX_TEMPLATE_GC_ENABLED=false` and the template endpoints fail with a
recorded build error instead of running.

For a stricter deployment, run OpenSandbox on a separate worker host or
Kubernetes cluster and point the `HARBORBOX_OPENSANDBOX_*` settings at it. None
of the above makes this an isolation boundary for hostile code: sandboxes are
still ordinary containers sharing the host kernel.

## Templates and warm starts

Every sandbox must specify one template. There is no generic image fallback:
this keeps dependency contents, resource sizing, and startup behaviour
predictable.

This repository ships exactly one image, `base` -- a minimal Debian with the
sandbox user and a writable `/workspace`, declared in
`templates/manifest.yaml`. It exists to be something worth starting `FROM` and
to give the warm pool somewhere to live. Everything else is a Dockerfile a
product sent.

Templates follow the same core model as E2B templates: Docker installs every
dependency during the build, and runtime requests only reference the resulting
versioned image. Build the base with:

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

Pools are keyed by template name in `HARBORBOX_WARM_POOL`, so a product's own
`custom-<hash>` image can be pooled exactly like the base:
`{"base":1,"custom-abc123def456":2}`. A warm start is worth roughly three
seconds, which is why this stayed configurable when products stopped having a
template each. The default is one base sandbox. After five minutes without
demand, idle pools scale to zero. The next request uses the direct path while the pool refills in the
background. Warm-pool maximums remain part of admission headroom so a refill
cannot push the host over its aggregate memory or CPU cap.

## Templates are Dockerfiles

`POST /v1/templates` takes a Dockerfile. That is the only way to build an
image, and the only shape this endpoint accepts.

Harborbox used to take an allowlisted `{base, apt, npm, env}` spec and generate
the Dockerfile itself, with a tailored base image per product baked into this
repository. That put every product's dependency upgrades behind a Harborbox
release, and it meant this repo had to know which pandas version Onvo wanted.
Products own their images now.

```bash
curl -sX POST localhost:8000/v1/templates -H "X-API-Key: $HARBORBOX_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "dockerfile": "FROM debian:bookworm-slim\nRUN apt-get update && apt-get install -y jq\n"
  }'
```

The name is `custom-<hash>`, hashed over the Dockerfile and the build context
together, so the endpoint stays idempotent on exactly the same terms as a
package spec: identical input returns `200` with the existing template, new
input returns `201` and starts a build, and a spec whose last build failed is
retried.

To `COPY` anything, upload a build context first. It is a gzipped tar posted
raw, stored content-addressed, and referenced by digest:

```bash
tar -czf ctx.tar.gz -C ./my-files .
```

```bash
curl -sX POST localhost:8000/v1/build-contexts -H "X-API-Key: $HARBORBOX_API_KEY" \
  -H 'Content-Type: application/gzip' --data-binary @ctx.tar.gz
```

Pass the returned digest as `context`. Without one the build gets an empty
context and any `COPY` fails, which is the safe default: a Dockerfile with no
context cannot reach anything on the build host.

`./scripts/try-custom-template.sh` runs that whole path end to end -- context
upload, build, sandbox, and a command proving a non-allowlisted package and a
copied file both arrived.

### What still constrains a Dockerfile

Package allowlists mean nothing once `RUN` is arbitrary, so what is checked is
the shape of the build rather than its contents:

- **`HARBORBOX_TEMPLATE_FROM_ALLOWLIST`** — repository prefixes a `FROM` may
  name, matched after Docker's implicit `docker.io/library/` expansion, on
  *every* `FROM` rather than the first. This is the supply-chain control and
  the difference between "any Dockerfile" and "any Dockerfile starting from an
  image we vetted". Harborbox's own static bases are always allowed.
- **`HARBORBOX_TEMPLATE_MAX_DOCKERFILE_BYTES`** and `_INSTRUCTIONS`, and
  `_MAX_CONTEXT_BYTES` / `_MAX_CONTEXT_FILES`. The context byte cap applies
  both to the upload and to the sum of the members' declared sizes, so an
  archive that compresses to nothing and expands to gigabytes is refused
  without being expanded.
- **`HARBORBOX_TEMPLATE_MAX_CONCURRENT_BUILDS`** — builds queue instead of all
  starting at once.
- Uploaded contexts may hold only regular files, directories, and symlinks that
  stay inside the tree. Absolute paths, `..` traversal, and device nodes are
  refused at upload.
- BuildKit refuses privileged build steps at the daemon: `RUN --security=insecure`
  fails even if `--allow security.insecure` reached the command line, because
  buildkitd is not started with that entitlement.

Harborbox appends a conformance layer to whatever you wrote -- uid/gid 10001,
a writable `/workspace` as `WORKDIR`, and `USER 10001:10001` last. This is the
analogue of E2B injecting `envd`: you decide what the image contains, Harborbox
guarantees it can still be run as a sandbox. **Your own trailing `USER` is
overridden**, and `FROM scratch` will not work -- no shell means no commands.

A template cold-starts unless it is named in `HARBORBOX_WARM_POOL`. That is the
right default: pooling every image a product ever built would trade a bounded
image count for an unbounded pool count.

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

const sandbox = await Sandbox.create("base", {
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

Every sandbox receives hard OpenSandbox memory and CPU limits. The scheduler
reserves those entire limits rather than assuming current low usage will
continue. A queued execution is admitted only when all three conditions hold:

```text
reserved sandbox memory + requested memory <= sandbox budget
host available memory   - requested memory >= emergency reserve
reserved cpu            + requested cpu    <= HARBORBOX_MAX_PARALLEL_CPU
```

The first and third are ledger checks against Harborbox's own reservations. The
second is the only one that looks at the real machine: it reads `MemAvailable`
from `/proc/meminfo` on every sweep, so unrelated host processes eating memory
block admission even when the ledger says there is room.

The effective sandbox budget is the smallest applicable ceiling derived from:

```text
HARBORBOX_SANDBOX_MEMORY_BUDGET_MB
host memory - host reserve - platform reserve
```

### What each status reserves

Memory and CPU are not held across the same set of states. A frozen sandbox
keeps its whole memory reservation and gives back its CPU allotment; a cold one
gives back both. That asymmetry is the entire reason the pause ladder exists.

| Status | Memory held | CPU held |
| --- | --- | --- |
| `created` | no | no |
| `starting` | yes | yes |
| `running` | yes | yes |
| `paused_memory` | yes | no |
| `paused_cold` | no | no |
| `pooling` / `pooled` | yes | no |
| `killed` / `failed` | no | no |

Reservation begins at `starting`, not at `running`: admission takes it before
any container exists.

### How many sandboxes run at once

Two numbers, and the smaller wins:

```text
by memory = floor((sandbox budget - warm pool memory) / sandbox memory)
by cpu    = floor((max parallel cpu - warm pool cpu)  / sandbox cpu)

concurrent running sandboxes = min(by memory, by cpu)
```

The warm pool's configured maximum is reserved permanently, even after an idle
pool scales to zero, so a refill cannot race admission over the cap.

On the bundled Compose stack with a 16 GiB Docker VM and nothing overridden:

| Sandbox shape | By memory | By cpu | Concurrent |
| --- | --- | --- | --- |
| `base` template, 512 MB / 1.0 cpu | 7 | 3 | **3** |
| custom template at the defaults, 1024 MB / 2.0 cpu | 3 | 1 | **1** |

CPU is the binding constraint in both cases, because `compose.yaml` defaults
`HARBORBOX_MAX_PARALLEL_CPU` to 4 and the warm pool holds 1.0 of it
permanently. If those numbers are lower than you expected, raise
`HARBORBOX_MAX_PARALLEL_CPU` toward the host's real core count, or size
sandboxes at 1.0 cpu. The memory budget is not what is stopping you.

Three counts that are not that one:

- **Sandboxes that exist** is far larger. `paused_cold` holds nothing, so
  cold-suspended sandboxes are bounded only by row retention.
- **Sandboxes holding memory** is `running` plus `starting` plus
  `paused_memory` plus the pool, per the table above.
- **Concurrent executions** is the running count times
  `HARBORBOX_MAX_CONCURRENT_EXECUTIONS_PER_SANDBOX`. A second command into an
  already-running sandbox adds zero memory and zero cpu at admission, so it is
  gated only by that per-sandbox limit. The container's hard limits still bound
  what the overlapping executions do together.

The `/v1/capacity` endpoint reports all of it live: budget, reservations,
warm-pool headroom, running sandboxes, and running and queued executions.

### Queue order

Queued executions are scanned oldest first, up to
`HARBORBOX_SCHEDULER_SCAN_LIMIT` rows per sweep, under `SELECT ... FOR UPDATE`
so nothing else can change them mid-decision. An execution that does not fit is
skipped and the scan continues, so small work is not stuck behind a large job
that cannot start yet.

One exception stops that from starving the large job. Once the row at the head
of the queue has waited longer than `HARBORBOX_QUEUE_AGING_SECONDS`, a failed
admission stops the sweep outright, and nothing behind it is admitted until it
goes. The queue is therefore opportunistic for the first minute of any job's
wait and strictly first-come after that.

Admitted executions run as independent tasks, but every sandbox start goes
through a per-sandbox lock, so two executions landing on the same cold sandbox
can never produce two containers. A caller that gives up waiting does not cancel
the start: it is shielded, because aborting mid-`start_sandbox` would strand a
container the sandbox row never learns about.

### Pause behavior

- `sandbox.pause(memory=True)` delegates to OpenSandbox pause. On Docker,
  processes survive and the full memory remains reserved. Resuming is an
  unfreeze.
- `sandbox.pause(memory=False)` creates an OpenSandbox filesystem snapshot and
  terminates the runtime. CPU and memory are released; files survive, live
  processes do not. Resuming builds a new container from the snapshot.

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

What the tier is worth, measured by `tests/e2e_pause_ladder.py` against a live
stack at 256 MB / 0.5 cpu:

```text
freeze     65 ms      snapshot   5075 ms
unfreeze   97 ms      restore     465 ms
```

Read the right-hand column before enabling it. The pause side is where the tier
overwhelmingly pays: freezing an idle sandbox costs almost nothing, while
snapshotting one is the single most expensive thing the scheduler does. The
resume side is a much narrower win than it looks, because restoring from a
snapshot is also sub-second. Freezing buys roughly 370 ms on resume in exchange
for holding the sandbox's entire memory reservation until it is used again --
worth having where a sandbox is likely to be touched again within the minute
and memory is not the binding constraint, and worth turning off where it is.

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

## Running on more than one machine

Harborbox reaches OpenSandbox over ordinary HTTP, so pointing it at a remote or
Kubernetes-backed OpenSandbox is a settings change: `HARBORBOX_OPENSANDBOX_DOMAIN`,
`_PROTOCOL` and `_API_KEY`. Nothing in the control plane assumes a local
runtime, and the `SandboxRuntime` protocol in `runtime_protocol.py` exists so
that another provider is an addition rather than a change to the scheduler,
admission controller or API. This is the recommended shape for hostile
workloads, since ordinary Docker containers share the host kernel.

Two parts of the control plane are already written for more than one instance.
Warm pools coordinate through PostgreSQL with distributed leases and leader
fencing (`postgres_pool_store.py`), and queue wake-ups use `LISTEN`/`NOTIFY` on
that same database specifically so they reach every API replica without adding a
broker.

Three things still stand between that and an actual multi-instance deployment,
in dependency order:

1. **One control-plane process only.** The single-start guarantee is an
   in-process `asyncio.Lock` per sandbox. Two replicas against one database hold
   two unrelated lock dictionaries, race the same sandbox row, and can create
   two containers for one sandbox. `Dockerfile.api`'s `uvicorn` has no
   `--workers` for exactly this reason. The fix is a database-level lock --
   `SELECT ... FOR UPDATE` on the sandbox row, the same pattern
   `_admit_available_jobs` already uses for admission.
2. **Capacity is measured locally.** `total_memory_mb` and
   `available_memory_mb` read `/proc/meminfo` inside the API container.
   `HARBORBOX_TOTAL_MEMORY_MB` can override the total, but the live emergency
   guard still reads local `MemAvailable`, which on a split deployment describes
   a machine that runs no sandboxes. A single flat budget also cannot express
   per-node capacity. Multi-node admission needs capacity to come from the
   runtime rather than from `/proc`.
3. **The registry has one address per deployment shape.** BuildKit pushes to
   `registry:5000` over the build network, while the reference Harborbox hands
   to OpenSandbox is `127.0.0.1:5050`, because the Docker daemon resolves it
   from the host. That pairing only holds while OpenSandbox is on the same host.
   A split or clustered deployment needs a real DNS name with TLS; see
   `docs/arbitrary-dockerfile-templates.md` section 9.

Until those land, a Kubernetes deployment is possible but comes with conditions:
one API process schedules the whole cluster, `HARBORBOX_TOTAL_MEMORY_MB` has to
be set deliberately because nothing can measure it, and the registry needs a
routable name.

There is also a design question worth settling before any of that is built. Once
Kubernetes is scheduling pods, one of the two schedulers is redundant. Either
Harborbox keeps the queue and admission and hands placement to the cluster, or
it steps back to being the durable queue and lifecycle API alone.

## Telemetry

Harborbox exports OTLP traces and logs. Both are off unless an endpoint is
configured:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://ingester.onvo.ai
OTEL_EXPORTER_OTLP_HEADERS=signoz-access-token=<ingestion key>
HARBORBOX_ENVIRONMENT=production
```

With no endpoint set, nothing is instrumented at all — no middleware, no
patched clients, and no exporter retrying an address nobody configured. That is
the intended state for local development, and pointing a local stack at the
real ingester is still safe: any `HARBORBOX_ENVIRONMENT` other than
`production` or `staging` exports under `harborbox-dev`, so it cannot be
mistaken for the deployment.

Two things about this are load-bearing rather than cosmetic:

- **The service name is `harborbox`, and it is a constant in
  `src/harborbox/telemetry.py` rather than an environment variable.** The
  estate's daily checkup asks SigNoz for error groups under that exact name.
  Any other value — a typo, an unset variable, the SDK's `unknown_service`
  default — makes the error count structurally zero and renders it as green.
  That is what happened for the first months of Harborbox's life, and it is
  why the wiring here is explicit instead of `opentelemetry-instrument`.
- **Logs matter as much as traces.** The failures worth catching happen in the
  scheduler's background loop and in template builds, long after any request
  span has ended. `logging` records go to the collector *and* to stderr, so
  `docker compose logs` is unaffected.

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

`POST /v1/sandboxes` requires `template` to name either the statically
configured `base` or a template registered through `POST /v1/templates`. An unknown name is a `422`; a known
name whose image is still building or has failed is a `409` carrying the build
status.

`GET /v1/templates` returns `{"templates": [...]}` covering both the static and
the derived registry. Static entries have a null `spec_hash` and a `status` of
`ready`.

`POST .../commands` and `.../processes` accept
`"wait": true`, which holds the connection until the execution finishes and
answers `200` with the result instead of `202` with a job id. If the execution
outlives the wait the response is still `202`, so a client that asked to wait
and ran out of patience simply polls as before. Both SDKs send it by default.

`PUT .../files/content?path=/tmp/file.bin` accepts
`application/octet-stream` and streams the request through the control plane.
It does not create a base64 copy of large uploads. File API absolute paths are
limited to `/workspace` and `/tmp`; other absolute roots and traversal are
rejected.

### Execution model

Python runs as an ordinary command. There is no interpreter service in the
sandbox and no cross-call state: upload a script and run it, or run
`python -c`, through `POST /v1/sandboxes/{id}/commands`.

`POST /v1/sandboxes/{id}/executions` was removed in 0.3.0 along with the
Jupyter kernel behind it. The kernel held one namespace per sandbox, which cost
~3 s of boot and ~197 MB resident in every sandbox that ran Python —
see `scripts/bench/` for the harness and `scripts/bench/results/` for the
measurements — and the endpoint had no caller: the TypeScript SDK only ever
read `GET /v1/executions/{id}`, and Onvo uploads a script and runs it through
`/commands`. `GET /v1/executions/{id}` and its `/events` and `/cancel`
siblings are unaffected; they serve commands and processes.

Where `/opt/forkrun.py` is present a script is forked from a daemon that has
already imported its heavy modules, keeping the ~1.5 s import off every call.
It is transparent — a forked run matches `python script.py`, including its
environment and exit code — and absent, the script simply runs normally.
Harborbox no longer ships it: a product that wants the fast Python path
installs it in its own Dockerfile, which is the same move as owning the rest
of the image.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy
uv run pytest
docker compose config
```
