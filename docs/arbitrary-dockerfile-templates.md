# Arbitrary Dockerfile templates

Status: **Phase 0 and Phase 1 implemented and verified on a live stack.**
Sections 3-6 are the original plan; sections 11 and 12 record what was actually
built and where it deviated. Phases 2 and 3 remain unbuilt.

Goal: let a caller submit a Dockerfile (and optionally a build context) to
`POST /v1/templates` and get back a template a sandbox can run — the thing E2B
templates do — instead of today's `{base, apt, npm, env}` spec with allowlisted
package names.

## 1. How E2B does it

**Original system.** The developer wrote a `Dockerfile` plus an `e2b.toml`, ran
`e2b template build`, and the CLI built the image with the *local* Docker
engine, then pushed it to E2B's private registry. The backend pulled it,
flattened the image filesystem into an ext4 rootfs, booted that rootfs in a
Firecracker microVM, ran the start command, waited on the ready command, and
snapshotted memory + disk. Sandboxes resume from the snapshot, which is where
the ~150 ms start comes from. The developer only had to know Dockerfiles; the
isolation came from the microVM, not the image.

**Build System 2.0 (current).** The Dockerfile stopped being the interface. The
template is now a fluent builder in the SDK:

```javascript
Template()
  .fromImage('node:24')
  .copy('src/', '.')
  .runCmd('npm install')
  .setStartCmd('node server.js', 'curl -f localhost:3000')
```

with `fromBaseImage()`, `fromTemplate()`, `setEnvs()`, `aptInstall()`,
`pipInstall()`, `gitClone()`, `setReadyCmd()`. Dockerfiles are a migration path
only — `Template().fromDockerfile()` and `e2b template migrate`. Builds run on
E2B's infrastructure, not locally, and the build environment is itself a full
sandbox. Templates are addressed by a mutable **tag** (`my-template-dev` vs
`my-template`), not by a content hash. Builds cap at 1 h; CPU/RAM/disk are
per-plan.

**Caching.** Every builder call is a layer, hashed over the command and its
inputs. File copies are hashed separately on content, mode, size, and relative
path, so invalidating a layer does not re-upload the files under it.
`.skipCache()` invalidates from that instruction forward, `build({skipCache})`
invalidates the whole template, `forceUpload` invalidates the file cache too.
E2B claims ~14× on a cache hit, ~2× cold.

**The two things that let them accept arbitrary user build steps:**

1. The build runs inside their own isolation primitive. There is no shared
   daemon socket handed to a build.
2. The artifact only ever runs in a Firecracker microVM, so a hostile image is
   contained by hardware virtualization rather than by what the Dockerfile was
   allowed to say.

Harborbox has neither today. That is the whole difficulty, and section 3 is
about buying back as much of (1) as a single-host Docker deployment can.

## 2. Where Harborbox stands

`POST /v1/templates` takes `{base, apt, npm, env}`. `render_dockerfile`
([templates.py:238](../src/harborbox/templates.py)) emits the Dockerfile;
the caller never writes one. Package names are regex-checked and matched
against `HARBORBOX_TEMPLATE_APT_ALLOWLIST` / `_NPM_ALLOWLIST`. The build is
`docker build -` with the Dockerfile on stdin and **no build context**, run by
`TemplateBuilder._build_sync`
([template_builder.py:159](../src/harborbox/template_builder.py)) against the
host Docker socket mounted into the API container. Naming is content-addressed:
`<base>-<sha256[:12]>`.

Two facts from the current deployment matter for the design:

- OpenSandbox injects its own execution daemon (`execd_image =
  "opensandbox/execd:v1.0.21"` in `compose.yaml`), and execd is PID 1 in the
  sandbox container. **A template image does not need to bake anything
  OpenSandbox-specific**, so an arbitrary `FROM` is feasible from the runtime's
  side.
- Images built by the API are only visible to OpenSandbox because both point at
  the same daemon. That coupling has already cost us: the note in
  `sandbox/Dockerfile` records Coolify's nightly `docker rmi` deleting a
  locally-built image that had "no registry to come back from", surfacing as
  `404 pull access denied` on sandbox create.

## 3. Phase 0 — move builds off the host daemon (prerequisite)

Add two services and remove the API's Docker socket.

```
api --buildctl--> buildkitd (rootless) --push--> registry <--pull-- opensandbox
```

- **`builder`**: `moby/buildkit:rootless`, on the `harborbox-control` network,
  no host socket, `mem_limit` / `cpus` / `pids_limit` set. Rootless BuildKit
  runs build steps under user namespaces, so a `RUN` in a caller's Dockerfile
  is not root on the host.
- **`registry`**: `registry:3` with htpasswd auth, on `harborbox-control`, its
  own volume. Derived images push to
  `registry:5000/harborbox/derived/<name>:<hash>`.
- **API**: replaces `docker build -` with `buildctl --addr tcp://builder:1234
  build --frontend dockerfile.v0 --output type=image,push=true,name=...`.
  Never passes `--allow security.insecure` or `--allow network.host`.
- **OpenSandbox**: pulls from `registry:5000`. Needs registry credentials in
  its config — see open question Q1.
- **Static templates** move to the same registry, which also fixes the Coolify
  sweep problem above and lets the `LABEL coolify.managed=true` workaround go
  away.
- **GC** changes from `docker image rm` to a registry manifest delete plus
  `registry garbage-collect`, and `buildctl prune --keep-storage` for the build
  cache. `TemplateBuilder.collect_unused_templates` keeps its current
  in-use/idle logic; only `_remove_image_sync` is rewritten.

The API keeps no Docker socket at all after this. That is the single largest
security change in the proposal and it is worth doing even if section 4 is
never built.

**If every caller is a trusted first-party service**, Phase 0 can be deferred
and section 4 built on the existing socket. It is not defensible for
customer-supplied Dockerfiles.

## 4. Phase 1 — raw Dockerfile and build context

### API

`POST /v1/build-contexts` (new). Streaming `application/gzip` tar upload,
content-addressed, returns `{"digest": "sha256:..."}`. Caps on total bytes,
file count, and per-file size; rejects absolute paths, `..` components, and
symlinks pointing outside the tree. Stored in a blob volume (or as an OCI
artifact in the registry), GC'd with the templates that reference it.

`POST /v1/templates` gains two mutually exclusive input modes:

```jsonc
// existing, unchanged
{"base": "relaydeck", "apt": ["chromium"], "npm": ["@playwright/mcp@0.0.78"]}

// new
{
  "dockerfile": "FROM python:3.12-slim\nRUN pip install polars\n",
  "context": "sha256:...",        // optional
  "build_args": {"VARIANT": "slim"},
  "memory_mb": 1024,
  "cpu": 2
}
```

`TemplateSpec` gains `dockerfile: str | None`, `context_digest: str | None`,
`build_args: dict[str, str]`, and `base` becomes optional. `canonical_json`
folds all of them in, so `spec_hash` and the existing idempotency contract —
`200` for an identical spec, `201` plus an async build for a new one, retry on a
previously failed one — carry over unchanged. Name becomes `custom-<hash12>`
when there is no base. `SandboxTemplate.spec` is already a JSON column, so no
migration beyond a nullable `context_digest` column if we want it queryable for
GC.

### What replaces the allowlists

Package allowlists are meaningless once arbitrary `RUN` exists. They are
replaced by constraints on the build, not on its contents:

| Control | Setting |
| --- | --- |
| Which registries a `FROM` may reference | `HARBORBOX_TEMPLATE_FROM_ALLOWLIST` (default: the three static bases + `docker.io/library/*`) |
| Raw mode enabled at all | `HARBORBOX_TEMPLATE_RAW_DOCKERFILE_ENABLED` (default `false`) |
| Which API keys may use it | new `templates:raw` scope in `security.py` |
| Dockerfile size / instruction count | `HARBORBOX_TEMPLATE_MAX_DOCKERFILE_BYTES`, `_MAX_INSTRUCTIONS` |
| Context size / file count | `HARBORBOX_TEMPLATE_MAX_CONTEXT_MB`, `_MAX_CONTEXT_FILES` |
| Wall clock | `HARBORBOX_TEMPLATE_BUILD_TIMEOUT_SECONDS` (exists, 1800) |
| Resulting image size | `HARBORBOX_TEMPLATE_MAX_IMAGE_MB`, checked from build metadata; over-size builds are recorded `failed` and the image deleted |
| Simultaneous builds | `HARBORBOX_TEMPLATE_MAX_CONCURRENT_BUILDS`, with a semaphore in `TemplateBuilder` — `schedule_build` currently spawns an unbounded task per request |

The `FROM` allowlist is the one that actually matters. It is the supply-chain
control, and it is the difference between "any Dockerfile" and "any Dockerfile
starting from an image we vetted".

### The conformance layer

`render_dockerfile` stops being the whole Dockerfile and becomes a wrapper. The
caller's text is emitted verbatim, then Harborbox appends a trailing stage that
enforces the runtime contract the three static bases satisfy today:

```dockerfile
# ---- caller's Dockerfile verbatim ----
# ---- harborbox conformance layer ----
USER root
RUN (getent group 10001 || groupadd --gid 10001 sandbox) \
 && (getent passwd 10001 || useradd --uid 10001 --gid 10001 \
       --home-dir /workspace --no-create-home sandbox) \
 && mkdir -p /workspace && chown 10001:10001 /workspace
COPY --from=<relaydeck base> /opt/forkrun.py /opt/forkrun.py
WORKDIR /workspace
USER 10001:10001
```

This is the equivalent of E2B injecting `envd`. It guarantees uid 10001, a
writable `/workspace`, and the fast Python path regardless of what the caller
wrote, and it is why `FROM scratch` still will not work (nor should it — no
shell means no commands).

A caller's own trailing `USER`/`WORKDIR` is overridden by design; document it.

## 5. Phase 2 — typed steps (optional)

E2B moved *away* from Dockerfiles for good reasons: typed steps give
per-instruction error attribution, a canonical hash, and per-step policy. Add
an ordered step list that compiles to the same Dockerfile:

```jsonc
{"base": "relaydeck", "steps": [
  {"env": {"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}},
  {"apt_install": ["chromium"]},
  {"run": "python -m pip install polars"},
  {"copy": {"context": "sha256:...", "src": "app/", "dest": "/workspace/app"}}
]}
```

Today's `{apt, npm, env}` becomes sugar over this and stays wire-compatible.
The value is that untrusted tenants can be restricted to the non-`run` steps
with the existing allowlists, while trusted ones get `run` or raw Dockerfiles —
one code path, two policies.

## 6. Phase 3 — caching and warm starts

- **Layer cache**: `--export-cache type=registry,ref=registry:5000/harborbox/buildcache`
  and the matching `--import-cache`. Specs sharing a prefix then reuse layers
  across templates, which is where E2B's 14× comes from. Harborbox today has
  whole-image dedup via the content hash but no incremental reuse.
- **Warm pools for derived templates**: currently excluded by design (bounded
  image count would become unbounded pool count). If wanted, opt-in `warm: N`
  per derived template with a global cap on total derived pool slots, counted
  in admission headroom the same way static pools already are.
- **Snapshot-at-ready**: E2B's `setStartCmd`/`setReadyCmd` is what makes their
  warm start meaningful. The equivalent here would be `POST
  /v1/sandboxes/{id}/pause` immediately after a template-declared ready probe,
  stored as a cold snapshot the pool resumes from. Larger than this document.

## 7. What this does not buy

This does not give microVM isolation. After all three phases a hostile
Dockerfile's `RUN` executes as an unprivileged user inside a rootless BuildKit
container, and the resulting sandbox still shares the host kernel through
ordinary Docker. Rootless BuildKit plus dropping the API's socket narrows the
blast radius a lot; it does not make this E2B-equivalent. For genuinely hostile
multi-tenant workloads the README's existing advice stands: run OpenSandbox on
Kubernetes with gVisor, Kata, or Firecracker.

## 8. Open questions

1. ~~**Q1** — Can the pinned OpenSandbox v0.2.2 pull from a private
   authenticated registry, and where do those credentials go in its
   `config.toml`?~~ **Answered — see section 9. Yes, but credentials are
   per-request, not config.** Phase 0 is unblocked, with two constraints
   recorded below.
2. ~~**Q2** — Does rootless BuildKit work on the deployment targets?~~
   **Answered for OrbStack — see section 10. Yes, on native overlayfs with the
   process sandbox intact.** *Still unverified on the Coolify host*; section 10
   lists the three commands to run there.
3. **Q3** — Do we want mutable tags (E2B's model: `my-template-dev`) alongside
   the content hash? Content hashing gives free dedup and idempotency; tags give
   callers a stable name across rebuilds. They can coexist, but only if we
   decide who owns the namespace.
4. **Q4** — Egress during builds. `pip install` needs the network; a build
   network policy is a separate control from the sandbox `egress` flag and
   currently does not exist.

## 9. Q1 spike result: private registry pull

Run against `opensandbox/server:v0.2.2` and Docker Engine 29.4.0 on OrbStack,
with a `registry:3` behind htpasswd basic auth. All spike containers, images,
and credentials were removed afterwards; the developer's `~/.docker/config.json`
was never touched (an isolated `DOCKER_CONFIG` was used for the push).

**Verdict: private authenticated registries work, but the credentials do not
live in `config.toml` — they are passed on every sandbox create.**

### Where the credentials go

There is no registry-auth setting in `[docker]` or anywhere else in
`server/configuration.md`, and grepping the shipped source confirms it. Auth is
a per-request field on the create body:

```
CreateSandboxRequest.image -> ImageSpec { uri, auth: ImageAuth { username, password } }
```

`DockerContainerOps._resolve_image_auth` turns that into docker-py's
`auth_config` and hands it to `images.pull`
(`opensandbox_server/services/docker/container_ops.py`). The official SDK
Harborbox already depends on exposes the same thing —
`SandboxImageSpec(uri=..., auth=SandboxImageAuth(username=..., password=...))`,
accepted by `Sandbox.create(image=...)`.

Harborbox change required: `OpenSandboxRuntime.start` currently passes a bare
image string (`self.settings.image_for_template(template)`,
[opensandbox_runtime.py:256](../src/harborbox/opensandbox_runtime.py)), which
the SDK coerces to a `SandboxImageSpec` with no auth. It must construct the
spec explicitly and attach credentials from new
`HARBORBOX_REGISTRY_USERNAME` / `_PASSWORD` settings. The warm-pool path
(`WarmPool`) builds its own create calls and needs the same treatment.

### Measured results

| Case | Result |
| --- | --- |
| Pull from authed registry, **no** credentials | `500 DOCKER::SANDBOX_IMAGE_PULL_FAILED` — `no basic auth credentials` |
| Pull from authed registry, **correct** credentials | `202`, sandbox `Running`, container up as `uid=10001(sandbox)` on Debian 12 |
| Same image, **wrong** credentials, image already cached | **`202`, sandbox starts** — see C1 |
| Registry addressed by container-network name (`spike-registry:5000`) | `500` — see C2 |

### C1 — credentials are only checked on a cache miss

`_ensure_image_available` calls `docker_client.images.get(uri)` first and
returns early on a hit; `_pull_image` runs only on `ImageNotFound` or a
platform mismatch. So once any tenant has pulled an image onto a host, any
later create for that same URI succeeds **with wrong or absent credentials**.

For Harborbox this is fine — one operator-owned registry, one credential, and
the same image store is already shared. It would not be fine if per-tenant
registry credentials were ever offered as a feature: registry auth here is not
a per-sandbox authorization boundary, and the spec should not present it as
one. Worth an explicit note in the README when Phase 0 lands.

### C2 — the registry must be reachable from the *host daemon*, not from the OpenSandbox container

The pull is executed by the Docker daemon over the mounted socket; OpenSandbox
only passes the URI through. A compose service name therefore does not resolve
— `spike-registry:5000` failed with `Get "https://spike-registry:5000/v2/":
Bad Gateway`, and note it also tried **HTTPS**, because only `localhost` and
`127.0.0.1` are on the daemon's implicit insecure list.

This changes section 3: the registry cannot simply be a service on
`harborbox-control` addressed as `registry:5000`. Options, in order of
preference:

1. Publish the registry on the host and address it as `127.0.0.1:5050/...`.
   Plain HTTP is accepted without touching daemon config (verified — the pull
   went out as `http://127.0.0.1:5050/v2/...`). Simplest for the single-host
   Compose deployment, and it works unchanged when OpenSandbox runs on the same
   host.
2. A real DNS name with TLS, for the split-host / Kubernetes deployment the
   README already recommends for hostile workloads.
3. Adding the registry to the daemon's `insecure-registries` — rejected, it
   edits host daemon config we otherwise never touch.

Phase 0 should assume (1) for Compose and document (2) for production. The
BuildKit push target and the OpenSandbox pull URI must be the *same string*, so
it is one setting, `HARBORBOX_REGISTRY_ENDPOINT`, consumed by both sides.

### Unaffected

`execd` is injected by OpenSandbox and is PID 1 regardless of the image, so a
registry-sourced image needs nothing baked in. The pulled sandbox ran as
`uid=10001` from the image's own `USER` line, which is the contract the
conformance layer in section 4 enforces. (`/opt/forkrun.py` is absent from
`relaydeck` — it comes from `sandbox/Dockerfile`, so the conformance layer's
`COPY --from` source is base-dependent, not universal.)

## 10. Q2 spike result: rootless BuildKit, and the full chain end to end

Run on OrbStack (kernel `7.0.11-orbstack`, Docker 29.4.0, `overlay2`,
`max_user_namespaces=24093`, `/dev/fuse` present) with
`moby/buildkit:rootless`. **Not yet run on the Coolify host** — see the bottom
of this section. All spike resources were removed afterwards.

**Verdict: rootless BuildKit works, and the whole Phase 0 + Phase 1 chain works
end to end — but only with the network topology in §10.3. The obvious topology
is unsafe.**

### 10.1 The builder

```
docker run -d --name builder \
  --security-opt seccomp=unconfined \
  --security-opt apparmor=unconfined \
  --security-opt systempaths=unconfined \
  --device /dev/fuse \
  moby/buildkit:rootless
```

- `snapshotter: overlayfs` — **native, not `fuse-overlayfs`**. No slow path.
- `process-mode:sandbox` — the process sandbox is **preserved**.
  `--oci-worker-no-process-sandbox`, which most rootless BuildKit guides reach
  for, is *not* needed; `--security-opt systempaths=unconfined` covers it and
  keeps build steps isolated from buildkitd itself. Do not copy the common
  recipe.
- `Privileged: false`, and buildkitd runs as `uid=1000(user)`.

### 10.2 What was actually built and run

A Dockerfile with arbitrary `RUN`s, `apt-get install jq` (**not** on
`HARBORBOX_TEMPLATE_APT_ALLOWLIST`), and its own `useradd` was built through
`buildctl`, pushed to the authenticated registry, then pulled and started by
OpenSandbox v0.2.2. Inside the running sandbox:

```
uid=10001(sandbox) gid=10001(sandbox)      # conformance contract holds
jq-1.6                                     # arbitrary package present
```

Build-time identity was `uid=0(root)` — namespaced root, mapped through the
user namespace to unprivileged uid 1000 on the host. That is the distinction
the design rests on and it held.

**Caching** (the Phase 3 claim): an identical rebuild returned 4 `CACHED` steps
in ~1 s against an original build dominated by `apt-get`. Appending a new final
layer left every earlier layer `CACHED`. Note this is buildkitd's *local* store,
so it dies with the container — the builder needs a named volume at minimum,
and the registry cache export in section 6 for anything better.

**Entitlements** hold on the daemon side, not just the client side:

| Attempt | Result |
| --- | --- |
| `RUN --security=insecure`, no `--allow` | rejected at parse — `unknown flag: security` |
| same, **with** `--allow security.insecure` | `granting entitlement security.insecure is not allowed by build daemon configuration` |

So the refusal comes from buildkitd's own config. The API cannot grant it by
accident and a caller cannot request it. That is stronger than the "never pass
`--allow`" instruction in section 3, and it should be stated as the mechanism.

### 10.3 The network topology is the security-critical part

Getting BuildKit and the host daemon to agree on a registry address is
awkward, because of C2 in section 9: BuildKit pushes from inside a container,
while the daemon pulls from the host.

**The obvious fix is unsafe.** Putting buildkitd on `--network host` makes both
sides agree on `127.0.0.1:5050` and it works — but a caller's build step then
shares the host network namespace. A `RUN curl http://127.0.0.1:5050/v2/`
returned `401` from inside a build, i.e. it *connected*. In the real
deployment that same reachability covers PostgreSQL and the Harborbox API on
host loopback. **Never run the builder with host networking.**

**The fix that works**: put buildkitd on an isolated bridge network whose only
other member is the registry, and let the two sides address the registry by
different names. A registry does not care about the host part of a reference —
it is only how the client dials it — so the same repository can be pushed and
pulled under different addresses. Verified:

```
push (from buildkitd, bridge alias):  registry:5000/hb/isolated:spike
pull (by host daemon, published port): 127.0.0.1:5050/hb/isolated:spike
```

The sandbox started from it, and the same `RUN curl http://127.0.0.1:5050/v2/`
now reports `UNREACHABLE` from inside a build.

This replaces the single `HARBORBOX_REGISTRY_ENDPOINT` proposed at the end of
section 9 with two settings:

- `HARBORBOX_REGISTRY_PUSH_ENDPOINT` — how BuildKit dials the registry
  (`registry:5000`), plus `registry.insecure=true` on the output, since BuildKit
  does **not** inherit the daemon's implicit localhost-is-insecure rule.
- `HARBORBOX_REGISTRY_PULL_ENDPOINT` — what goes in `SandboxTemplate.image` and
  therefore in the create request (`127.0.0.1:5050`).

The repository path after the host part must be identical on both sides. Worth
a test that asserts exactly that, because a mismatch fails only at sandbox
create, long after the build reported success.

#### What broke this, and what fixes it

Everything above is true of `compose.yaml` on a laptop, and
`test_the_builder_reaches_the_registry_and_nothing_else` asserted it for
months. **It was false in the Coolify deployment the whole time**, measured on
the infrastructure host:

```
builder networks:  <uuid>=172.22.0.3   <uuid>_build=172.19.0.3
                   ^^^^^^^^^^^^^^^^^ Coolify's project network, not in compose

from inside the builder:
  api:8000/health   -> {"status":"ok"}
  opensandbox:8080  -> HTTP/1.1 401 Unauthorized
  postgres:5432     -> OPEN
```

A build step shares buildkitd's network namespace, so a caller-supplied `RUN`
could reach the control plane and its database. The cause was Coolify, not the
compose file: it appends its project network to every service of a compose
application. `connect_to_docker_network` was already **false** for that
application and does not prevent it.

The reason the test could not see it is worth stating plainly, because it is
the more transferable lesson: **a compose file is not the deployment.** A test
that reads one file and asserts about one network asserts something about the
file. Everything the orchestrator adds afterwards is invisible to it.

Two exits were considered and one was tried:

1. **Isolate the build steps rather than the container**, with `[worker.oci]
   networkMode = "cni"`. Attempted, and it does not work here, for three
   independent reasons. `moby/buildkit:rootless` ships no CNI plugins and no
   `/etc/buildkit`. Adding them still fails — `could not add "hbbuild0":
   operation not permitted` — because rootlesskit has no `NET_ADMIN` without
   `slirp4netns`, which this image also lacks. And even with `slirp4netns` the
   build traffic NATs out *through the container*, which is itself on the
   control-plane network, so a separate namespace does not remove the
   reachability it was supposed to remove. (An aside for anyone who tries
   anyway: `cniConfigPath` wants a single plugin config, not a conflist; a
   conflist fails with `error parsing configuration: missing 'type'`.)
2. **Stop Coolify attaching the network.** Nothing in this repository can, and
   a setting that a future Coolify honours is one a future Coolify can also
   ignore.

**The fix that shipped is neither: make the attachment harmless.** The builder
moved into its own Compose project, `compose.builder.yaml`, deployed as a
second Coolify application. The project network Coolify appends to it therefore
joins the builder to the only other service in that project, which is nothing —
it holds one service, and `test_coolify_can_only_attach_the_builder_to_itself`
keeps it that way. The only other network the builder joins is the shared
external `harborbox-build`, whose members are the registry and the gateway
below.

The cost is the unix socket. The API drove buildkitd over
`harborbox-buildkit-run:/run/user/1000`, which needs one host *and one compose
project*; that was the strongest form of this isolation, because it meant the
two shared no network at all, and it cannot survive the split. So the API dials
TCP:

- `builder` listens on `tcp://0.0.0.0:1234` **and** the unix socket. buildkitd
  applies TLS only to `tcp://` listeners, so the socket stays as the
  container-local healthcheck path and the TCP listener is the authenticated
  one.
- `[grpc.tls] ca` in `buildkitd.toml` is the load-bearing line: with a CA set,
  buildkitd uses `RequireAndVerifyClientCert`. Without it, it logs `TLS is not
  enabled ... enabling mutual TLS authentication is highly recommended` and
  accepts anyone who can open a socket. That is not hypothetical here — see the
  gateway below — so `Settings` refuses to start with a `tcp://` builder
  address and no client certificate.
- `buildkit-gateway` (haproxy, TCP passthrough) sits in the Harborbox project
  on both `build` and `control`, and the API dials *it*. The API never joins
  `build`. This is the same dual-homing the registry has always had, and it is
  safe for the same reason: Docker does not route between bridge networks, so a
  build step that reaches the gateway reaches that listener and nothing behind
  it. What stops a build step *using* the listener is the client certificate it
  does not have.
- `scripts/gen-buildkit-certs.sh` issues the pair into a directory on the host —
  CA (mounted by nothing), server (builder only), client (API only). No
  certificate, key, or CA is in this repository, and the CA key never enters a
  running container.

Nothing about DNS changed and nothing about it could be allowed to: the builder
is still a container on user-defined bridges, so `[dns] nameservers =
["127.0.0.11"]` still resolves for build steps, which is what keeps `pip
install` working on a host that firewalls 8.8.8.8. This is also the second
reason the CNI route was wrong — a per-build namespace is exactly where that
resolver stops being valid.

#### What is true now, and what is not

Measured from inside a real build step by
`tests/e2e_build_isolation.py`, on the split stack:

```
reachable from inside a build step:
  registry:5000            REACHED
  api:8000                 unreachable
  harborbox-api:8000       unreachable
  harborbox:8000           unreachable
  postgres:5432            unreachable
  opensandbox:8080         unreachable
```

That test is the answer to "the old test passed while production was open". It
builds a template whose Dockerfile opens sockets and reads the results out of a
sandbox created from the resulting image, so it measures the deployment rather
than the file. It **requires `registry:5000` to answer**: a probe that reaches
nothing proves nothing, and without that control the test would pass just as
happily if `timeout` were missing from the base image. Its negative control was
run by hand — `docker network connect harborbox-control <builder>`, which is
what Coolify did — and it reproduces the production measurement exactly:
`api:8000 REACHED`, `postgres:5432 REACHED`, `opensandbox:8080 REACHED`.

What remains, stated so nobody reads more into this than it says:

- **A build step still shares a network with the registry and the gateway**, by
  construction. The registry is what a build is *for*; the gateway is bounded
  by the client certificate. Both are on `harborbox-build`, and anything else
  put on that network is put within reach of every caller's `RUN`.
- **Host-published ports are still reachable**, as they are from any container
  on any bridge: a build step can dial the host gateway address. On the
  infrastructure host that means Traefik's 80/443. The registry publishes on
  `127.0.0.1` only, which a bridge cannot route to, so it is not among them.
  The wider internet is reachable by design — builds install packages.
- **The separation is only as good as the deployment.** If the builder
  application is ever given a domain in Coolify, Coolify attaches the proxy
  network to it and the boundary is gone; the same is true if a second service
  is added to `compose.builder.yaml`, or if someone folds the builder back into
  the main application. The unit tests pin the file; only the e2e probe pins
  the host.
- **This is not a sandbox for hostile code at build time.** It bounds what a
  build step can *reach*. What it can do inside its own namespace is the
  rootless-BuildKit story in 10.1, and sandboxes themselves are still ordinary
  containers sharing the host kernel.

### 10.4 The Coolify host needs one root change before the builder runs

The OrbStack result did not transfer, exactly as this section warned it might.
On `infrastructure` (91.99.169.190) the builder crash-looped from the first
deploy:

```
[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: permission denied
[rootlesskit:parent] This error might have happened because
  /proc/sys/kernel/apparmor_restrict_unprivileged_userns is set to 1
```

Ubuntu 23.10 and later ship `kernel.apparmor_restrict_unprivileged_userns=1`,
which stops an unconfined process creating a user namespace. Rootless BuildKit
is exactly that process, so it cannot start.

`apparmor=unconfined` is already set on the service and does not help — under
this restriction "unconfined" is precisely the category being denied.

**The profile BuildKit's own error message suggests does not work here.** It
tells you to write `/etc/apparmor.d/usr.bin.rootlesskit`, which attaches to
that path as a *host* binary. The rootlesskit that matters runs inside a
container, under whatever profile Docker gives that container, and never
transitions into it. Installed and loaded on this host, the builder kept
crash-looping with the identical error — that part is solid, because the
builder does run with `seccomp=unconfined`.

### What fixed it

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/99-rootless-userns.conf
```

The builder came up healthy on the next restart, with `using overlayfs` and
`process-mode:sandbox` — the two values section 10 wanted — and a caller
Dockerfile then built and ran end to end (section 10.5).

This lifts the restriction for every unconfined process on the box, not just
this container, which is worth weighing on a shared host.

### What is *not* established, so nobody repeats the experiment badly

`deploy/apparmor/harborbox-buildkit` and the `HARBORBOX_BUILDER_APPARMOR`
variable are a container profile granting `userns`. They are kept because that
is the targeted fix in principle — but **whether they work was never actually
determined here**, and the first version of this section wrongly claimed they
had been disproven.

The probes behind that claim were confounded: they set only
`--security-opt apparmor=...` and left Docker's *default seccomp* profile in
place, and that profile independently blocks `unshare(CLONE_NEWUSER)`. So every
"denied" they produced is explained by seccomp alone and says nothing about
AppArmor. A valid probe needs both:

```bash
docker run --rm --security-opt seccomp=unconfined --security-opt apparmor=<profile> \
  alpine:3.21 unshare -Ur echo OK
```

What is real evidence against the profile route on this host: the kernel
(6.8.0-136-generic) exposes no `userns` entry in
`/sys/kernel/security/apparmor/features/`, which suggests it has no AppArmor
userns mediation for a profile rule to hook into. Suggestive, not proven.
Check that directory before choosing between the profile and the sysctl.

After either, confirm what actually came up:

```bash
docker logs <builder-container> 2>&1 | grep -E 'snapshotter|process-mode|error'
```

Wanted: `using overlayfs` (not `fuse-overlayfs`) and `process-mode:sandbox`.
`fuse-overlayfs` still builds, only slower. If buildkitd will not start without
`--oci-worker-no-process-sandbox`, that is a material weakening — build steps
stop being isolated from buildkitd — and Phase 1 should not serve untrusted
callers on that host without revisiting it.

Until this is done the API stays healthy and every other service runs, but no
template can be built: `POST /v1/templates` has nowhere to send the build. The
symptom in Coolify is the application sitting at `restarting:unknown` while its
`api` container reports healthy.

### 10.5 The chain, verified on the deployed stack

With the builder up, a caller Dockerfile was posted to the deployed API and
taken all the way to a running command:

```
POST /v1/templates  {"dockerfile": "FROM debian:bookworm-slim\nRUN echo built-in-prod > /proof.txt"}
  -> custom-7e5afd13b6d3, status building -> ready in ~4s
POST /v1/sandboxes  {"template": "custom-7e5afd13b6d3"}
  -> sbx_..., created
POST /v1/sandboxes/{id}/commands  "cat /proof.txt; id -u; id -un; pwd"
  -> exit 0, queued 16ms, startup 862ms, execution 1051ms
     stdout: built-in-prod / 10001 / sandbox / /workspace
```

That covers the parts unit tests cannot: BuildKit built it, pushed it to the
registry over the `build` network, the host daemon pulled it through the
loopback endpoint, and the conformance layer put the process at uid 10001 in
`/workspace` with the caller's own layer present.

**One real limitation surfaced.** The first attempt used `FROM alpine:3.21` and
failed in the conformance layer:

```
/bin/sh: groupadd: not found
```

`CONFORMANCE_LAYER` uses `groupadd`/`useradd`, which are Debian-family tools;
Alpine has `addgroup`/`adduser` from BusyBox. The `FROM` allowlist permits
`docker.io/library/*`, so a caller may legitimately pick Alpine and get a build
failure four lines into a layer they did not write. Either detect the base and
branch, or document Debian-family bases as a requirement and reject others up
front — the current behaviour is the worst of both.

## 11. Phase 0 as built

Implemented. The whole chain was exercised against a live stack, and doing so
caught two things no unit test would have:

1. **buildctl authenticates the push from the client's docker config.**
   buildkitd keeps no credentials; the client forwards them over the session.
   The first real run built cleanly and died on
   `HEAD /v2/.../blobs/sha256:...`. `TemplateBuilder._registry_credentials`
   now writes a throwaway `config.json` into the build's temp directory and
   points `DOCKER_CONFIG` at it, so the plaintext password dies with the build.
2. **The API cannot reach the registry at the pull endpoint.** That endpoint is
   loopback *on the Docker host*; inside the API container it addresses the
   API's own loopback. The template collector's manifest delete now goes
   through the push endpoint, and the registry is dual-homed on `build` and
   `control` so both sides can reach it. Docker does not route between bridge
   networks, so this does not put the builder within reach of the control
   plane.

A third finding is a deployment note rather than a bug: **BuildKit resolves the
base image through the client's session**, so the API container needs egress to
whatever registry a `FROM` names. A client on `--network none` fails at
`load metadata` before the build starts.

Verified end to end: build on rootless BuildKit over the unix socket → push to
the authenticated registry as `registry:5000/...` → pull the same repository as
`127.0.0.1:5050/...` → run it (`uid=10001`, the built marker file present) →
reclaim it, leaving `tags: null` in the registry.

### Deviations from the plan in section 3

- **A unix socket, not TCP.** Section 3 proposed `buildctl --addr tcp://...`.
  That would require the API and the builder to share a network, and build
  steps run inside buildkitd's network namespace, so that network would be
  reachable from every caller's build. The socket lives in a shared volume and
  needs no shared network -- verified with a client on `--network none`. It has
  to be mounted at `/run/user/1000`, which the image already owns as uid 1000;
  a fresh named volume inherits that, and anywhere else buildkitd dies with
  `bind: permission denied`.

  **Reversed later, and the reasoning above is still right.** A socket needs
  one compose project as well as one host, and the deployed builder had to
  leave the project to stop Coolify's project network putting the control plane
  within reach of every build (10.3). It is TCP now, with mutual TLS and a
  dual-homed gateway the API dials instead of joining the build network. The
  socket survives as buildkitd's second listener, for the healthcheck. Note
  what the trade actually was: not "socket vs TCP" on the merits, where the
  socket wins, but "a stronger mechanism inside a topology that did not hold"
  against "a weaker one inside a topology that does".
- **Two settings, not one.** `HARBORBOX_REGISTRY_ENDPOINT` became
  `_PUSH_ENDPOINT` and `_PULL_ENDPOINT`, for the reason in section 10.3.
  `tests/test_compose_deployment.py` asserts they differ only in the host part.
- **Static bases move to the registry, and that is not optional.** Section 3
  listed it as a bonus. It is a requirement: BuildKit resolves a derived
  template's `FROM` over its own network and cannot see the host daemon's image
  store. `docker-bake.hcl` gained `TEMPLATE_REGISTRY`, and a test pins the tags
  it builds against what `Settings.image_for_template` resolves.
- **The local-daemon path is kept, not removed.** With
  `HARBORBOX_BUILDER_ADDRESS` unset, builds still go through `docker build -`
  against a mounted socket. Existing deployments keep working; the API image
  therefore still carries the Docker CLI.

### Not done

- `scripts/build-templates.sh --push` is wired and its tags are pinned by a
  test, but **it has not been run against the real registry** -- only
  `docker buildx bake --print` was checked. Someone should push the three
  bases once before trusting a derived build on a fresh host.
- Coolify's nightly `docker rmi` sweep should now be survivable, since images
  come back from the registry rather than being lost. The keeper containers and
  the `coolify.managed=true` label are still in place and were **not** removed;
  that cleanup wants its own change and a night's observation.
- Q2 remains unverified on the Coolify host (section 10.4).

## 12. Phase 1 as built

Implemented. `POST /v1/templates` takes a `dockerfile`, `POST /v1/build-contexts`
takes a tarball for it to `COPY` from, and `./scripts/try-custom-template.sh`
drives the whole path.

Verified end to end against a live stack: a Dockerfile installing `jq` -- which
is deliberately *not* on the apt allowlist -- plus a `COPY` from an uploaded
context and an `ENV`, built, pushed, pulled, and ran. Inside the sandbox:

```
{"user":"sandbox","greeting":"phase-one"}
{"copied_from_context": true, "argv": ["it-works"]}
```

`user: sandbox` is uid 10001, from the appended conformance layer rather than
from anything the caller wrote. Both refusals were checked live too: a `FROM`
outside the allowlist returns 422 naming the allowed prefixes, and a tarball
containing `../../etc/passwd` is refused at upload.

### Decisions worth knowing

- **`custom-<hash>` is its own namespace.** A raw template is derived from no
  base, so `<base>-<hash>` would name a relationship that does not exist.
  `base_of_derived_template` deliberately does not match it.
- **Package-spec hashes did not change.** `canonical_json` omits `dockerfile`
  and `context` when absent rather than emitting nulls, so upgrading does not
  invalidate and rebuild every derived template already in the registry. The
  pre-existing canonical-JSON contract test still passes untouched, which is
  what proves it.
- **The conformance layer is appended, not merged**, so a caller's own trailing
  `USER` does not decide who the sandbox runs as. `getent` guards the
  `groupadd`/`useradd` so building on a Harborbox base -- which already has uid
  10001 -- is not a build failure.
- **Every `FROM` is checked, not just the first**, after Docker's implicit
  `docker.io/library/` expansion, and stage names (`FROM build`) are exempt
  because they refer to earlier stages rather than something to pull.
- **Context caps apply twice**: to the uploaded bytes and to the sum of the
  members' declared sizes, so an archive that compresses to nothing and expands
  to gigabytes is refused without being expanded.
- **Builds queue.** `template_max_concurrent_builds` defaults to 2. Before raw
  Dockerfiles this mattered less -- an allowlisted apt install is bounded work.

### Not done

- **Phase 2** (typed step list) and **Phase 3** (registry layer cache, warm
  pools for derived templates) are untouched.
- **`build_args`** is not implemented; `ARG`/`ENV` inside the Dockerfile covers
  the same ground.
- **`HARBORBOX_TEMPLATE_MAX_IMAGE_MB`** from section 4 is not implemented, so a
  caller can still build a very large image. Disk is bounded only by the
  template collector's idle sweep.
- **Per-API-key scoping** is not implemented: raw Dockerfiles are on or off for
  the whole deployment, not per caller.
- Q2 on the Coolify host (section 10.4) is still unverified, and it gates
  turning this on there.

## 13. Migrating a deployment to the two-application split

This changes how Harborbox is deployed: **two Coolify applications instead of
one**, on the same host. What follows is the order that works. Deploying the
main application first is not fatal -- template builds fail while the builder
is missing, and recover on their own once it is up -- but nothing else here is
optional.

The reason for each step is in 10.3; this is the runbook.

**1. Create the shared bridge, on the host.**

```bash
docker network create harborbox-build
```

Both applications declare it `external:` and neither creates it. Compose fails
by name if it is missing, which is the intended failure.

**2. Issue the buildkit certificates, on the host.**

```bash
cd /path/to/a/harborbox/checkout   # any checkout of this commit
./scripts/gen-buildkit-certs.sh
```

It writes `ca/`, `server/` and `client/` under
`$HARBORBOX_BUILDKIT_TLS_DIR` (default `/data/harborbox/buildkit-tls`), which
both applications bind-mount. **Not Docker volumes**: Coolify rewrites a
service's named-volume reference to `<app-uuid>_<name>` and creates that volume
empty, so the builder mounted an empty `/certs` and died on `open
/certs/cert.pem: no such file or directory`. Neither `external: true` nor an
explicit `name:` on the declaration prevents it — both survive into the
generated compose and are both ignored for the mount. Bind mounts pass through
untouched, which is how the docker socket has always reached opensandbox. Set
the variable identically on both applications if you move it. It needs Docker
and nothing else --
`openssl` runs inside `python:3.12-slim-bookworm`. Re-running is a no-op;
`--force` reissues and invalidates the running pair, so it needs both
applications restarted afterwards.

**3. Create the builder application in Coolify.**

- Same git repository, same branch.
- Docker Compose location: `/compose.builder.yaml`.
- Environment: nothing is required. Set `HARBORBOX_BUILDER_APPARMOR` only if
  this host needs the profile route from 10.4 rather than the sysctl.
- **Give it no domain.** A domain makes Coolify attach the proxy network to
  it, and every network the builder joins is one a caller's build step joins.
  That would undo the entire point of the split.

Deploy it, then confirm what came up:

```bash
docker logs <builder-container> 2>&1 | grep -E 'snapshotter|process-mode|TLS|server on'
```

Wanted: `using overlayfs`, `process-mode:sandbox`, `running server on
[::]:1234`, and `TLS is disabled for unix://...` — that last line is about the
healthcheck socket and is expected. **`TLS is not enabled for tcp://...` is
not expected**: it means buildkitd found no certificates and is accepting
anyone who can open a socket. Stop and fix step 2 if you see it.

**4. Redeploy the main application from this commit.**

Its compose file no longer defines `builder`, so Coolify removes that
container, and gains `buildkit-gateway`. Nothing else about the application
changes: same volumes, same database, same registry, same published loopback
port.

**5. Confirm the chain, then confirm the boundary.**

```bash
# builds still work
curl -sS -X POST https://<api>/v1/templates -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"dockerfile":"FROM debian:bookworm-slim\nRUN apt-get update"}'
# ... poll GET /v1/templates/<name> until ready

# and the isolation the split is for, measured from inside a build step
HARBORBOX_API_KEY=$KEY uv run pytest -m e2e tests/e2e_build_isolation.py
```

The second is the one that matters. It fails loudly if the builder ends up on
a network with the control plane again, which is the failure this whole
exercise is about and the one that a green unit-test suite did not catch.

**6. Clean up, once builds are proven.**

The old socket volume is now unreferenced:

```bash
docker volume rm harborbox_harborbox-buildkit-run
```

The old Coolify-managed `<uuid>_build` network is likewise orphaned and will be
pruned. Leave `harborbox-buildkit` (the build cache) alone -- it moved to the
builder project, so the old one is stale, but removing it only costs a cold
first build.

**Rolling back** means redeploying both applications from a commit before this
one and putting `builder` back in the main application. The registry, the
database and every built image are untouched by either direction; the only
state this migration introduces is the certificate volumes, and a rollback
simply stops using them.

## Sources

- [Introducing Build System 2.0 — E2B Blog](https://e2b.dev/blog/introducing-build-system-2-0)
- [Template quickstart — E2B Docs](https://docs.e2b.dev/template/quickstart)
- [Template caching — E2B Docs](https://docs.e2b.dev/template/caching)
- [Scaling Firecracker: Using OverlayFS to Save Disk Space — E2B Blog](https://e2b.dev/blog/scaling-firecracker-using-overlayfs-to-save-disk-space)
