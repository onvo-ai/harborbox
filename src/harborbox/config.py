import re
from functools import lru_cache
from typing import Literal

from opensandbox.models.sandboxes import SandboxImageAuth, SandboxImageSpec
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The one statically registered template. Everything else is a Dockerfile a
# product brought itself.
BASE_TEMPLATE = "base"

# A template built from a caller's Dockerfile. The digest keeps the image name a
# pure function of the template name, so resolution never reads the database.
CUSTOM_TEMPLATE_NAME = re.compile(r"^custom-[0-9a-f]{12}$")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARBORBOX_",
        env_file=".env",
        extra="ignore",
    )

    api_key: str = "change-me"
    execution_secret_key: SecretStr = SecretStr(
        "local-development-secret-change-me"
    )
    database_url: str = "postgresql+asyncpg://harborbox:harborbox@postgres/harborbox"
    opensandbox_domain: str = "opensandbox:8080"
    opensandbox_protocol: Literal["http", "https"] = "http"
    opensandbox_api_key: SecretStr = SecretStr("change-me-opensandbox")
    opensandbox_use_server_proxy: bool = True
    opensandbox_ready_timeout_seconds: float = Field(default=30.0, gt=0)
    # Bounds the live get_sandbox_info() lookup _detect_memory_exceeded makes
    # on every runtime error (14 call sites) to work out whether a failure was
    # an OOM kill. It is a diagnostic, not a critical path: without its own
    # short bound it inherits ConnectionConfig.request_timeout
    # (opensandbox_ready_timeout_seconds, 30s by default), which would let a
    # degraded control plane add up to another 30s on top of every error --
    # worst exactly when callers can least afford to wait longer.
    oom_diagnostic_timeout_seconds: float = Field(default=3.0, gt=0)
    opensandbox_snapshot_timeout_seconds: float = Field(default=300.0, gt=0)
    docker_base_url: str | None = None
    # The one image Harborbox ships. Products no longer get a template each:
    # they own a Dockerfile and POST it, which is the only way to build now.
    # This exists to be something to build FROM, and to give a warm pool
    # somewhere to live.
    base_image: str = "harborbox-sandbox-base:local"
    template_image_prefix: str = "harborbox-sandbox"
    template_version: str = "local"

    # Address of the rootless BuildKit daemon, e.g. "tcp://builder:1234". Set
    # it and template builds stop touching the Docker socket entirely; leave it
    # unset and they keep going through `docker build -` against the local
    # daemon, which is the pre-registry behaviour.
    builder_address: str | None = None
    # Both endpoints address the same registry and must agree on everything
    # after the host part; see Settings.push_image_for_template and
    # docs/arbitrary-dockerfile-templates.md section 10.3. Unset on both means
    # no registry: images stay in the local daemon's store.
    #
    # The push endpoint is how the builder dials the registry over the build
    # network; the pull endpoint is how the *Docker daemon* reaches it from the
    # host, which is why they differ in the bundled Compose deployment.
    registry_push_endpoint: str | None = None
    registry_pull_endpoint: str | None = None
    registry_username: str | None = None
    registry_password: str | None = None
    # BuildKit does not inherit the Docker daemon's implicit
    # "localhost is insecure" rule, so a plain-HTTP registry has to be declared.
    registry_insecure: bool = True
    registry_timeout_seconds: float = Field(default=30.0, gt=0)
    sandbox_network: str = "harborbox-net"
    sandbox_egress_network: str | None = None
    sandbox_runtime: str | None = None

    total_memory_mb: int | None = Field(default=None, ge=512)
    host_memory_reserve_percent: int = Field(default=25, ge=5, le=90)
    host_memory_reserve_min_mb: int = Field(default=1024, ge=256)
    platform_memory_reserve_mb: int = Field(default=512, ge=128)
    emergency_available_memory_mb: int = Field(default=512, ge=128)
    sandbox_memory_budget_mb: int | None = Field(default=None, ge=128)

    default_sandbox_memory_mb: int = Field(default=512, ge=128)
    max_sandbox_memory_mb: int = Field(default=4096, ge=128)
    default_sandbox_cpu: float = Field(default=1.0, gt=0)
    max_sandbox_cpu: float = Field(default=4.0, gt=0)
    max_parallel_cpu: float | None = Field(default=None, gt=0)
    sandbox_pids_limit: int = Field(default=128, ge=16)
    sandbox_tmpfs_mb: int = Field(default=128, ge=16)

    # Reaper — reclaims sandboxes nothing will come back for. See reaper.py.
    reaper_enabled: bool = True
    reaper_interval_seconds: int = Field(default=300, ge=10)
    # A sandbox starts lazily on its first execution, so `created` is normal
    # briefly. Fifteen minutes is far longer than any legitimate start.
    reaper_stuck_created_after_seconds: int = Field(default=900, ge=60)
    # `starting` is a RESERVED_SANDBOX_STATES member, so a start that gets
    # abandoned there -- rather than the primary fix in
    # `Scheduler._ensure_running` catching it -- holds real capacity, unlike
    # a stuck `created` row. Shorter than reaper_stuck_created_after_seconds
    # on purpose: five minutes is generous over every start budget that
    # feeds into it (opensandbox_ready_timeout_seconds and
    # lazy_start_wait_timeout_seconds combined top out well under this), so
    # anything still `starting` this long is abandoned, not slow. Defence in
    # depth, not the primary fix.
    reaper_stuck_starting_after_seconds: int = Field(default=300, ge=30)
    # Long enough that a failure is still there when someone comes to look.
    reaper_failed_retention_hours: int = Field(default=24, ge=1)

    warm_pool_enabled: bool = True
    # Keyed by template name, so a `custom-<hash>` image a product built can be
    # pooled exactly like the base. This used to be one field per product
    # template, which stopped working the moment products started bringing
    # their own images -- and warm starts are worth ~3s, so losing them for
    # everything but the base would have been a real regression.
    warm_pool: dict[str, int] = Field(default_factory=lambda: {"base": 1})
    warm_pool_warmup_concurrency: int = Field(default=2, ge=1, le=16)
    warm_pool_reconcile_seconds: float = Field(default=2.0, gt=0)
    warm_pool_idle_ttl_seconds: int = Field(default=900, ge=60)
    warm_pool_release_after_inactivity_seconds: int = Field(default=300, ge=0)
    warm_pool_acquired_timeout_seconds: int = Field(default=86_400, ge=60)
    warm_pool_release_on_shutdown: bool = True

    base_template_memory_mb: int = Field(default=512, ge=128)
    base_template_cpu: float = Field(default=1.0, gt=0)
    # Repository prefixes a caller's `FROM` may name, matched after Docker's
    # implicit `docker.io/library/` expansion. This is the supply-chain control:
    # `RUN` can install anything, so what a build *starts from* is the only part
    # still worth constraining. The static bases are appended automatically --
    # callers should always be able to build on our own templates.
    template_from_allowlist: frozenset[str] = frozenset({"docker.io/library"})
    template_max_dockerfile_bytes: int = Field(default=65_536, ge=256)
    template_max_dockerfile_instructions: int = Field(default=200, ge=1)
    # Build contexts. The byte cap applies twice: to the uploaded archive, and
    # to the sum of its members' declared sizes, so a tarball that compresses to
    # nothing and expands to gigabytes is refused without being expanded.
    template_max_context_bytes: int = Field(default=33_554_432, ge=1024)
    template_max_context_files: int = Field(default=2000, ge=1)
    template_context_root: str = "/var/lib/harborbox/build-contexts"
    template_max_concurrent_builds: int = Field(default=2, ge=1, le=32)
    template_build_timeout_seconds: float = Field(default=1800.0, gt=0)
    template_gc_enabled: bool = True
    template_gc_max_idle_days: int = Field(default=14, ge=1)
    template_gc_interval_seconds: float = Field(default=3600.0, gt=0)

    max_queue_depth: int = Field(default=1000, ge=1)
    max_concurrent_executions_per_sandbox: int = Field(default=1, ge=1, le=32)
    # Not the tick it used to be. The scheduler is woken by a notification when
    # something is enqueued (see notify.py); this is the fallback timeout that
    # makes a missed notification cost one interval instead of stalling, and
    # the interval on which deferred work is rescanned for capacity.
    scheduler_poll_seconds: float = Field(default=0.25, gt=0)
    # How long an SSE stream waits before emitting a keepalive. Completion
    # arrives by notification, so this only bounds silence on the wire.
    execution_stream_keepalive_seconds: float = Field(default=15.0, gt=0)
    scheduler_scan_limit: int = Field(default=100, ge=1)
    queue_aging_seconds: int = Field(default=60, ge=1)
    # Caps a queued command's payload. Named for the code endpoint it was
    # written for; kept under that name so existing deployments' env vars
    # keep working.
    max_code_bytes: int = Field(default=262_144, ge=1024)
    max_output_bytes: int = Field(default=8_388_608, ge=1024)
    max_upload_bytes: int = Field(default=157_286_400, ge=1024)
    default_execution_timeout_seconds: int = Field(default=30, ge=1)
    max_execution_timeout_seconds: int = Field(default=600, ge=1)
    default_idle_timeout_seconds: int = Field(default=300, ge=0)

    # The hot tier of the idle ladder. A sandbox idle for this long is frozen
    # (`paused_memory`) rather than snapshotted: CPU is released, the container
    # and its warm interpreter survive, and resuming is an unfreeze rather than
    # a fresh container built from a snapshot. It goes cold at
    # `idle_timeout_seconds` as before, so this only inserts a cheap-to-undo
    # step in front of the expensive one. 0 disables the tier and restores the
    # previous running -> paused_cold behaviour.
    hot_pause_idle_seconds: int = Field(default=60, ge=0)
    # A frozen sandbox still holds its full memory reservation, so without a cap
    # the hot tier would quietly consume the admission headroom that live work
    # needs. Freezing stops at this total and everything above it goes straight
    # to cold. 0 disables the tier the same way as above.
    hot_pause_budget_mb: int = Field(default=2048, ge=0)
    # Budget for an HTTP request (a file operation, or a PATCH that touches the
    # sandbox) that lands on a not-yet-running sandbox and triggers the same
    # lazy start command and process creation have always benefited from. Sized
    # to match the full cold-start budget a first execution gets end to end:
    # container create plus health check, which since the kernel's removal is
    # all there is to wait for. If it elapses the start is not aborted, only the
    # caller's wait: see `Scheduler.ensure_sandbox_ready`.
    lazy_start_wait_timeout_seconds: float = Field(default=60.0, gt=0)
    reaper_poll_seconds: float = Field(default=5.0, gt=0)
    # How many started-but-unassigned sandboxes to keep ready. 0 disables the
    # pool entirely, which is the old behaviour: every caller pays the container
    # start. Each pooled sandbox holds its memory reservation but no CPU budget.
    keep_completed_jobs_seconds: int = Field(default=86_400, ge=60)

    @property
    def docker_kwargs(self) -> dict[str, str]:
        return {"base_url": self.docker_base_url} if self.docker_base_url else {}

    @property
    def template_images(self) -> dict[str, str]:
        return {BASE_TEMPLATE: self.base_image}

    def derived_template_image(self, template: str) -> str:
        return f"{self.template_image_prefix}-{template}:{self.template_version}"

    def _qualify(self, endpoint: str | None, reference: str) -> str:
        """Address `reference` through `endpoint`, leaving the repository path alone.

        The endpoint is only how a client dials the registry, so push and pull
        may use different ones for the same store. Everything after the host
        must therefore stay byte-identical between the two -- see
        `push_image_for_template`.
        """
        return f"{endpoint}/{reference}" if endpoint else reference

    def push_image_for_template(self, template: str) -> str:
        """Return the reference BuildKit pushes to, dialled from its own network.

        Differs from `image_for_template` only in the host part. The Docker
        daemon that later pulls the image reaches the same registry by a
        different address, and a mismatch below the host would only surface at
        sandbox create, long after the build reported success.
        """
        return self._qualify(self.registry_push_endpoint, self._unqualified_image(template))

    def entrypoint_for_template(self, template: str | None) -> list[str]:  # noqa: ARG002 - see docstring
        """Return what opensandbox runs as the sandbox's bootstrap command.

        Not the image's CMD — opensandbox ignores that and runs whatever the
        create request passes, so this is the only place a sandbox's long-lived
        process is decided.

        Every template now idles. This used to branch: anything that ran Python
        started a Jupyter server here, because execd runs bash itself but
        proxied Python to a server nothing else would start. That server cost
        ~3 s of boot and ~197 MB resident in every sandbox to serve one
        endpoint, `POST /v1/sandboxes/{id}/executions`, which has since been
        removed outright -- it had no caller. Nothing in the sandbox needs a
        long-lived process of its own any more, so there is no branch to make.

        The `template` argument is kept because callers pass it and a future
        template may want its own bootstrap; it is deliberately unused today.
        """
        return ["tail", "-f", "/dev/null"]

    def is_custom_template(self, template: str) -> bool:
        """Whether this names a raw-Dockerfile template.

        Everything that is not the statically registered base is one of
        these: a Dockerfile some product sent.
        """
        return CUSTOM_TEMPLATE_NAME.fullmatch(template) is not None

    def is_known_template_name(self, template: str) -> bool:
        """Whether the name is well-formed. Existence and readiness need the DB."""
        return (
            template in self.template_images or self.is_custom_template(template)
        )

    def _unqualified_image(self, template: str | None) -> str:
        if template is None:
            message = "a registered sandbox template is required"
            raise KeyError(message)
        image = self.template_images.get(template)
        if image is not None:
            return image
        if self.is_custom_template(template):
            return self.derived_template_image(template)
        raise KeyError(template)

    def image_for_template(self, template: str | None) -> str:
        """Return the reference stored on the template row and sent to opensandbox.

        This is the pull side: opensandbox passes it straight through to the
        Docker daemon, which resolves it from the host rather than from any
        container network.
        """
        return self._qualify(self.registry_pull_endpoint, self._unqualified_image(template))

    @property
    def effective_from_allowlist(self) -> frozenset[str]:
        """The configured prefixes, plus every image this deployment ships.

        A caller building on `harborbox-sandbox-relaydeck` is using a base we
        built and pushed ourselves, so requiring an operator to allowlist it by
        hand would only ever produce a confusing refusal.
        """
        ours = {
            self._qualify(self.registry_push_endpoint, image).rsplit(":", 1)[0]
            for image in self.template_images.values()
        }
        ours |= {
            self._qualify(
                self.registry_push_endpoint, f"{self.template_image_prefix}-"
            ).rstrip("-")
        }
        return self.template_from_allowlist | ours

    def image_spec_for_template(self, template: str | None) -> SandboxImageSpec | str:
        """Return what to hand opensandbox as the image for a create call.

        opensandbox accepts registry credentials only per request -- it has no
        server-side credential store -- so a private registry means every
        create, warm-pool refill included, carries the auth. A deployment with
        no registry keeps passing the bare name, which is what the local daemon
        wants.
        """
        image = self.image_for_template(template)
        if not (self.registry_username and self.registry_password):
            return image
        return SandboxImageSpec(
            image,
            auth=SandboxImageAuth(
                username=self.registry_username, password=self.registry_password
            ),
        )

    @property
    def template_resources(self) -> dict[str, tuple[int, float]]:
        return {BASE_TEMPLATE: (self.base_template_memory_mb, self.base_template_cpu)}

    @property
    def warm_pool_sizes(self) -> dict[str, int]:
        """Pool sizes by template name, base and custom images alike.

        A product's own `custom-<hash>` template can be pooled by naming it
        here. Nothing validates that the name exists: a pool for a template
        that was never built simply never fills, which is the same outcome as
        setting it to zero and far better than refusing to start.
        """
        if not self.warm_pool_enabled:
            return dict.fromkeys(self.warm_pool, 0)
        return dict(self.warm_pool)

    def resources_for_template(self, template: str | None) -> tuple[int, float]:
        """Return static sizing for a template name.

        Any per-template override lives in the database and is applied by
        `harborbox.templates.resolve_template`, which is async and therefore the
        only place allowed to read it.
        """
        if template is None:
            message = "a registered sandbox template is required"
            raise KeyError(message)
        resources = self.template_resources.get(template)
        if resources is not None:
            return resources
        if self.is_custom_template(template):
            # A product's own image has no static sizing: the row it was
            # created with carries it, and `resolve_template` applies that.
            # These defaults only matter before the row is read.
            return (self.default_sandbox_memory_mb, self.default_sandbox_cpu)
        raise KeyError(template)

    @model_validator(mode="after")
    def validate_warm_pool_budget(self) -> "Settings":
        for template, (memory_mb, cpu) in self.template_resources.items():
            if memory_mb > self.max_sandbox_memory_mb:
                message = f"{template} template memory exceeds max sandbox memory"
                raise ValueError(message)
            if cpu > self.max_sandbox_cpu:
                message = f"{template} template CPU exceeds max sandbox CPU"
                raise ValueError(message)

        # Over the pools actually configured, not over the registered
        # templates: a pool may now name a product's own `custom-<hash>`
        # image, which has no static entry. `resources_for_template` falls back
        # to the deployment default for those, which is the same sizing the
        # pool will really reserve until its row is read.
        warm_memory = sum(
            count * self.resources_for_template(template)[0]
            for template, count in self.warm_pool_sizes.items()
        )
        warm_cpu = sum(
            count * self.resources_for_template(template)[1]
            for template, count in self.warm_pool_sizes.items()
        )
        if (
            self.sandbox_memory_budget_mb is not None
            and warm_memory > self.sandbox_memory_budget_mb
        ):
            message = "warm pool exceeds the aggregate sandbox memory budget"
            raise ValueError(message)
        if self.max_parallel_cpu is not None and warm_cpu > self.max_parallel_cpu:
            message = "warm pool exceeds the aggregate sandbox CPU budget"
            raise ValueError(message)

        """
        A pool that fits is not the same as a pool that leaves room.

        If the warm pool reserves everything but a sliver, any template larger
        than that sliver can never be admitted: its executions queue on
        `waiting_for: cpu` forever, because the budget is held by an idle pool
        that never yields it. That is a deadlock, and it presents as silence —
        a sandbox stuck in `created` with no error anywhere.

        Onvo Lite hit exactly this: pool 3.0, ceiling 4.0, onvo-lite needs 2.0.
        The check above passed it.

        "Largest" is the biggest template this configuration knows about: the
        registered base, plus anything named in the warm pool. Deliberately not
        `max_sandbox_cpu` -- that is a per-sandbox *ceiling*, not a size anyone
        asks for, and treating it as the largest template makes the check
        refuse the bundled defaults (a 1.0 pool against a 4.0 budget fails on a
        4.0 "largest"). A product sizes its own template per request and is
        bounded by admission at runtime, which is the right place for it.
        """
        largest_template_cpu = max(
            (self.resources_for_template(template)[1] for template in self.warm_pool_sizes),
            default=0.0,
        )
        largest_template_cpu = max(
            largest_template_cpu,
            *(cpu for _, cpu in self.template_resources.values()),
        )
        if (
            self.max_parallel_cpu is not None
            and warm_cpu + largest_template_cpu > self.max_parallel_cpu
        ):
            message = (
                f"warm pool leaves no CPU headroom: reserves {warm_cpu} of "
                f"{self.max_parallel_cpu}, but the largest template needs "
                f"{largest_template_cpu}. Raise max_parallel_cpu to at least "
                f"{warm_cpu + largest_template_cpu} or shrink the pool."
            )
            raise ValueError(message)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
