"""The ASGI entry point: `uvicorn api.runtime:app`.

Launch settings travel through the environment rather than a module global, because `--reload`
re-imports this module in a *child process* — anything assigned in the parent would be gone, and
the reloaded server would quietly serve a different log than the one asked for.
"""

from __future__ import annotations

import json
import os

from .main import create_app

ENV_VAR = "C2M_API_ARGS"


def _settings():
    from .__main__ import build

    raw = os.environ.get(ENV_VAR)
    argv = json.loads(raw) if raw else []
    settings, _host, _port, _reload = build(argv)
    return settings


app = create_app(_settings())
