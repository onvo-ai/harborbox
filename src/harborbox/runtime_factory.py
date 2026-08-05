from harborbox.config import Settings
from harborbox.runtime import DockerRuntime
from harborbox.runtime_protocol import SandboxRuntime


def create_runtime(settings: Settings) -> SandboxRuntime:
    if settings.runtime_provider == "docker":
        return DockerRuntime(settings)

    from harborbox.opensandbox_runtime import OpenSandboxRuntime

    return OpenSandboxRuntime(settings)
