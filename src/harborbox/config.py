from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HARBORBOX_",
        env_file=".env",
        extra="ignore",
    )

    api_key: str = "change-me"
    database_url: str = "postgresql+asyncpg://harborbox:harborbox@postgres/harborbox"
    docker_base_url: str | None = None
    sandbox_image: str = "harborbox-sandbox:local"
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

    default_sandbox_memory_mb: int = Field(default=512, ge=128)
    max_sandbox_memory_mb: int = Field(default=4096, ge=128)
    default_sandbox_cpu: float = Field(default=1.0, gt=0)
    max_sandbox_cpu: float = Field(default=4.0, gt=0)
    max_parallel_cpu: float | None = Field(default=None, gt=0)
    sandbox_pids_limit: int = Field(default=128, ge=16)
    sandbox_tmpfs_mb: int = Field(default=128, ge=16)

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
    reaper_poll_seconds: float = Field(default=5.0, gt=0)
    # How many started-but-unassigned sandboxes to keep ready. 0 disables the
    # pool entirely, which is the old behaviour: every caller pays the container
    # start. Each pooled sandbox holds its memory reservation but no CPU budget.
    sandbox_pool_size: int = Field(default=0, ge=0)
    sandbox_pool_poll_seconds: float = Field(default=2.0, gt=0)
    keep_completed_jobs_seconds: int = Field(default=86_400, ge=60)

    @property
    def docker_kwargs(self) -> dict[str, str]:
        return {"base_url": self.docker_base_url} if self.docker_base_url else {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
