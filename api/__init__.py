"""HTTP API over the analysis package.

The split is deliberate: `analysis` stays standard-library only and pure, so a backtest needs no
dependencies and no server. This package adds the web surface and nothing else — every number it
returns comes from `analysis`, unchanged.
"""

__all__ = ["create_app"]


def __getattr__(name: str):  # keep `import api` free of the FastAPI dependency
    if name == "create_app":
        from .main import create_app

        return create_app
    raise AttributeError(name)
