import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Derived template names are `<static base>-<12 hex chars>`. The suffix is the
# truncated canonical-spec digest, so a derived image name is a pure function of
# the template name: image resolution never needs to read the database.
DERIVED_TEMPLATE_SUFFIX = re.compile(r"^[0-9a-f]{12}$")

# Every name here is verified to resolve on Debian bookworm, which is what the
# static base images are built from. An allowlisted name that does not exist on
# bookworm would only ever surface as a failed build.
DEFAULT_APT_ALLOWLIST = frozenset(
    {
        "ca-certificates",
        "chromium",
        "default-mysql-client",
        "fonts-liberation",
        "fonts-noto-core",
        "postgresql-client",
        "redis-tools",
    }
)
DEFAULT_NPM_ALLOWLIST = frozenset({"@playwright/mcp"})


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
    runtime_provider: Literal["opensandbox", "docker"] = "opensandbox"
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
    sandbox_image: str = "harborbox-sandbox:local"
    onvo_pro_image: str = "harborbox-sandbox-onvo-pro:local"
    onvo_lite_image: str = "harborbox-sandbox-onvo-lite:local"
    relaydeck_image: str = "harborbox-sandbox-relaydeck:local"
    template_image_prefix: str = "harborbox-sandbox"
    template_version: str = "local"
    # Must match JUPYTER_HOST baked into sandbox/Dockerfile: execd reads the
    # host from the image's env, and this decides what actually listens.
    sandbox_jupyter_port: int = Field(default=8888, ge=1, le=65535)
    templates_without_python: frozenset[str] = frozenset({"relaydeck"})
    # DEBUG makes the sandbox's Jupyter log every request, which is the only
    # way to see what execd actually asks for from outside the binary.
    sandbox_jupyter_log_level: str = "INFO"
    # Cold sandboxes race their own Jupyter server; execd only retries for
    # ~12s. Measured cold start is well under a minute, so this is generous
    # rather than tuned — a sandbox that has not got a kernel by now is broken,
    # not slow.
    sandbox_python_ready_timeout_seconds: float = Field(default=120.0, gt=0)
    sandbox_network: str = "harborbox-net"
    sandbox_egress_network: str | None = None
    sandbox_runtime: str | None = None
    sandbox_agent_port: int = 8080
    sandbox_agent_connect_timeout_seconds: float = 30.0

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
    # feeds into it (opensandbox_ready_timeout_seconds,
    # lazy_start_wait_timeout_seconds, sandbox_python_ready_timeout_seconds
    # combined tops out well under this), so anything still `starting` this
    # long is abandoned, not slow. Defence in depth, not the primary fix.
    reaper_stuck_starting_after_seconds: int = Field(default=300, ge=30)
    # Long enough that a failure is still there when someone comes to look.
    reaper_failed_retention_hours: int = Field(default=24, ge=1)

    warm_pool_enabled: bool = True
    warm_pool_relaydeck: int = Field(default=2, ge=0, le=32)
    warm_pool_onvo_pro: int = Field(default=1, ge=0, le=32)
    warm_pool_onvo_lite: int = Field(default=0, ge=0, le=32)
    warm_pool_warmup_concurrency: int = Field(default=2, ge=1, le=16)
    warm_pool_reconcile_seconds: float = Field(default=2.0, gt=0)
    warm_pool_idle_ttl_seconds: int = Field(default=900, ge=60)
    warm_pool_release_after_inactivity_seconds: int = Field(default=300, ge=0)
    warm_pool_acquired_timeout_seconds: int = Field(default=86_400, ge=60)
    warm_pool_release_on_shutdown: bool = True

    relaydeck_template_memory_mb: int = Field(default=256, ge=128)
    relaydeck_template_cpu: float = Field(default=0.5, gt=0)
    onvo_pro_template_memory_mb: int = Field(default=1024, ge=128)
    onvo_pro_template_cpu: float = Field(default=1.0, gt=0)
    onvo_lite_template_memory_mb: int = Field(default=1024, ge=128)
    onvo_lite_template_cpu: float = Field(default=1.0, gt=0)

    # Derived templates are built from caller-supplied package lists, so the
    # request body is hostile input: every name is regex-checked and must also
    # appear in these allowlists before it reaches a generated Dockerfile.
    template_apt_allowlist: frozenset[str] = DEFAULT_APT_ALLOWLIST
    template_npm_allowlist: frozenset[str] = DEFAULT_NPM_ALLOWLIST
    template_max_apt_packages: int = Field(default=40, ge=0, le=200)
    template_max_npm_packages: int = Field(default=20, ge=0, le=200)
    template_max_env_vars: int = Field(default=32, ge=0, le=200)
    template_max_env_value_length: int = Field(default=1024, ge=1, le=8192)
    template_build_timeout_seconds: float = Field(default=1800.0, gt=0)
    template_gc_enabled: bool = True
    template_gc_max_idle_days: int = Field(default=14, ge=1)
    template_gc_interval_seconds: float = Field(default=3600.0, gt=0)

    max_queue_depth: int = Field(default=1000, ge=1)
    max_concurrent_executions_per_sandbox: int = Field(default=1, ge=1, le=32)
    scheduler_poll_seconds: float = Field(default=0.25, gt=0)
    scheduler_scan_limit: int = Field(default=100, ge=1)
    queue_aging_seconds: int = Field(default=60, ge=1)
    max_code_bytes: int = Field(default=262_144, ge=1024)
    max_output_bytes: int = Field(default=8_388_608, ge=1024)
    max_upload_bytes: int = Field(default=157_286_400, ge=1024)
    default_execution_timeout_seconds: int = Field(default=30, ge=1)
    max_execution_timeout_seconds: int = Field(default=600, ge=1)
    default_idle_timeout_seconds: int = Field(default=300, ge=0)
    # Budget for an HTTP request (a file operation, or a PATCH that touches the
    # sandbox) that lands on a not-yet-running sandbox and triggers the same
    # lazy start `create_execution` has always benefited from. Sized to match
    # the full cold-start budget a first execution gets end to end (container
    # create + agent health), not the much larger kernel-ready budget below --
    # these callers never touch the kernel. If it elapses the start is not
    # aborted, only the caller's wait: see `Scheduler.ensure_sandbox_ready`.
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
        return {
            "onvo-pro": self.onvo_pro_image,
            "onvo-lite": self.onvo_lite_image,
            "relaydeck": self.relaydeck_image,
        }

    def base_of_derived_template(self, template: str) -> str | None:
        """Return the static base a derived template name refers to, if it is one.

        Purely lexical, and deliberately so: derived image names are
        content-addressed by construction, which is what lets the synchronous
        callers below stay synchronous.
        """
        base, _, digest = template.rpartition("-")
        if base in self.template_images and DERIVED_TEMPLATE_SUFFIX.fullmatch(digest):
            return base
        return None

    def derived_template_image(self, template: str) -> str:
        return f"{self.template_image_prefix}-{template}:{self.template_version}"

    def entrypoint_for_template(self, template: str | None) -> list[str]:
        """Return what opensandbox runs as the sandbox's bootstrap command.

        Not the image's CMD — opensandbox ignores that and runs whatever the
        create request passes, so this is the only place a sandbox's long-lived
        process is decided.

        It has to be a Jupyter server for anything that runs Python. execd
        executes bash itself but proxies Python to a Jupyter server it reaches
        via JUPYTER_HOST/JUPYTER_TOKEN, and nothing else starts one: not execd,
        not the opensandbox server, and not the image, whose CMD is discarded.
        Both call sites here passed `tail -f /dev/null`, which produces a
        sandbox that starts, reports healthy, runs bash, and fails every Python
        execution with a 500 whose only clue is execd logging an empty
        "Jupyter server host is: ".

        Templates that never run Python keep the idle command — relaydeck is
        sized at 256 MB and installs no Jupyter to start.
        """
        base = self.base_of_derived_template(template or "") or template
        if base in self.templates_without_python:
            return ["tail", "-f", "/dev/null"]
        return [
            # Absolute, not `jupyter`: opensandbox execs this through its
            # bootstrap script, and the venv is only on PATH for the image's
            # own entrypoint.
            "/opt/venv/bin/jupyter",
            "server",
            "--ip=127.0.0.1",
            f"--port={self.sandbox_jupyter_port}",
            "--no-browser",
            # Empty token: auth off. execd needs JUPYTER_TOKEN set to believe a
            # Python runtime exists, but never sends it, so a server that
            # enforces one answers 403 and execd reports "no kernel specs
            # found". These flags must live here and not only in the image's
            # CMD — opensandbox discards the image CMD and runs exactly this
            # list, so a flag added there alone does nothing.
            "--IdentityProvider.token=",
            "--ServerApp.disable_check_xsrf=True",
            f"--ServerApp.log_level={self.sandbox_jupyter_log_level}",
        ]

    def is_known_template_name(self, template: str) -> bool:
        """Whether the name is well-formed. Existence and readiness need the DB."""
        return (
            template in self.template_images
            or self.base_of_derived_template(template) is not None
        )

    def image_for_template(self, template: str | None) -> str:
        if template is None:
            message = "a registered sandbox template is required"
            raise KeyError(message)
        image = self.template_images.get(template)
        if image is not None:
            return image
        if self.base_of_derived_template(template) is not None:
            return self.derived_template_image(template)
        raise KeyError(template)

    @property
    def template_resources(self) -> dict[str, tuple[int, float]]:
        return {
            "relaydeck": (
                self.relaydeck_template_memory_mb,
                self.relaydeck_template_cpu,
            ),
            "onvo-pro": (
                self.onvo_pro_template_memory_mb,
                self.onvo_pro_template_cpu,
            ),
            "onvo-lite": (
                self.onvo_lite_template_memory_mb,
                self.onvo_lite_template_cpu,
            ),
        }

    @property
    def warm_pool_sizes(self) -> dict[str, int]:
        if not self.warm_pool_enabled:
            return dict.fromkeys(self.template_images, 0)
        return {
            "relaydeck": self.warm_pool_relaydeck,
            "onvo-pro": self.warm_pool_onvo_pro,
            "onvo-lite": self.warm_pool_onvo_lite,
        }

    def resources_for_template(self, template: str | None) -> tuple[int, float]:
        """Return static sizing for a template name.

        A derived template inherits its base's sizing here. Any per-template
        override lives in the database and is applied by
        `harborbox.templates.resolve_template`, which is async and therefore the
        only place allowed to read it.
        """
        if template is None:
            message = "a registered sandbox template is required"
            raise KeyError(message)
        resources = self.template_resources.get(template)
        if resources is not None:
            return resources
        base = self.base_of_derived_template(template)
        if base is not None:
            return self.template_resources[base]
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

        warm_memory = sum(
            self.warm_pool_sizes[template] * resources[0]
            for template, resources in self.template_resources.items()
        )
        warm_cpu = sum(
            self.warm_pool_sizes[template] * resources[1]
            for template, resources in self.template_resources.items()
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
        """
        largest_template_cpu = max(
            (cpu for _, cpu in self.template_resources.values()), default=0.0
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
