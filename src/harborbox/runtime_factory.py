from harborbox.config import Settings
from harborbox.runtime import DockerRuntime
from harborbox.runtime_protocol import SandboxRuntime


def create_runtime(settings: Settings) -> SandboxRuntime:
    if settings.runtime_provider == "docker":
        return DockerRuntime(settings)

    # Deferred: opensandbox_runtime pulls in the opensandbox SDK and
    # code_interpreter, which most deployments (runtime_provider == "docker")
    # never touch and shouldn't pay import cost for at process startup.
    from harborbox.opensandbox_runtime import OpenSandboxRuntime  # noqa: PLC0415

    return OpenSandboxRuntime(settings)
