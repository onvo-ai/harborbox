"""HTTP driver for the e2e suite. Not a shipped package.

Harborbox's supported clients are the REST API and the TypeScript SDK. This
lived in `src/harborbox_sdk` and was published in the wheel; it is kept only
because the e2e suite drives a live Compose stack through it, and rewriting
312 lines of tests into raw HTTP calls would make them harder to read for no
gain. `pyproject.toml` puts `tests/` on the pytest path so these tests can
import it.

Do not grow this into a product surface, and do not document it: anything a
caller should be able to do belongs in the REST API and the TypeScript SDK.
"""

from live_client.client import SandboxClient
from live_client.models import Execution, ExecutionError, ExecutionResult, Logs, Sandbox

__all__ = [
    "Execution",
    "ExecutionError",
    "ExecutionResult",
    "Logs",
    "Sandbox",
    "SandboxClient",
]
