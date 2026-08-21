"""The process entrypoint: `uvicorn harborbox.main:app`.

Telemetry is wired here rather than in `api.py` because it has to happen
between the app being built and the first request being served.
`FastAPIInstrumentor.instrument_app` adds ASGI middleware, and Starlette
freezes its middleware stack the first time the app is called -- so doing this
from the lifespan would be too late, and doing it at `api.py` import time would
instrument every unit test that imports the app.
"""

import os

from harborbox.api import app
from harborbox.config import get_settings
from harborbox.telemetry import instrument

instrument(app, get_settings(), os.environ)

__all__ = ["app"]
