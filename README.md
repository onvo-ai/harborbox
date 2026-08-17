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

Remove the `unix://` prefix. The bundled registry uses basic auth, so create a
credentials file for it before the first start:

```bash
mkdir -p auth && docker run --rm httpd:2-alpine htpasswd -Bbn harborbox "$HARBORBOX_REGISTRY_PASSWORD" > auth/htpasswd
```

Use the same username and password you set for `HARBORBOX_REGISTRY_USERNAME`
and `HARBORBOX_REGISTRY_PASSWORD` in `.env`. Then start the registry, build the
immutable runtime templates and push them to it, build the API, and start:

```bash
docker compose up -d registry
```

```bash
./scripts/build-templates.sh --push
```

```bash
docker compose build api && docker compose up -d
```

The templates must be pushed, not merely built: the builder resolves each
derived template's `FROM` over its own network and cannot see the host daemon's
local image store.

The health endpoint is available at `http://localhost:8000/health` and API docs
at `http://localhost:8000/docs`.

The sandbox image services are behind an inactive `build` profile. They are
image build targets, not runtime services. Normal startup runs PostgreSQL,
Harborbox and the pinned OpenSandbox server.

OpenSandbox receives the host Docker socket in the bundled single-host setup,
which grants that service host-level Docker control. **Harborbox's API does
not.** It builds derived template images by driving a rootless BuildKit daemon
(`builder`) over a unix socket in a shared volume, so the control plane can ask
for an image and cannot start a container. A leaked `HARBORBOX_API_KEY` is
therefore not by itself arbitrary code execution on the host.

Three properties of that arrangement are deliberate, and each was verified
against a live stack rather than assumed — the evidence is in
`docs/arbitrary-dockerfile-templates.md` section 10:

- **The builder joins only the `build` network**, with the registry. Build
  steps run inside buildkitd's own network namespace, so any network the
  builder can reach is one a caller's build step can reach. Under
  `network_mode: host` a build step reached a service on host loopback, which
  on a single-host deployment means PostgreSQL and the API itself.
- **The API drives it over a socket, not TCP**, which is what lets the two
  share no network at all.
- **The registry has two addresses for one store.** BuildKit dials
  `registry:5000` over the build network; the reference Harborbox stores and
  hands to OpenSandbox is `127.0.0.1:5050`, because the *Docker daemon*
  resolves it from the host, where `registry` means nothing. Everything after
  the host part must stay identical, or the build succeeds and the sandbox
  create that follows fails on a missing image.

Leaving `HARBORBOX_BUILDER_ADDRESS` unset keeps the previous behaviour: builds
go through a mounted Docker socket against the local daemon, and the API holds
host-level Docker control again. If you use neither, set
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
Executions within one sandbox may overlap up to
`HARBORBOX_MAX_CONCURRENT_EXECUTIONS_PER_SANDBOX`; the container's hard memory
and CPU limits still bound their combined usage.

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
