"""What the server was started with. Set once at launch, never from a request."""

from __future__ import annotations

from dataclasses import dataclass, field

from analysis.config_defaults import PipelineConfig


# The launch flags travel to the ASGI app through the environment; `--reload` re-imports the app in
# a child process, where a module global set by the parent would be gone.
ENV_VAR = "C2M_API_ARGS"


@dataclass(frozen=True)
class Settings:
    """Read-only by construction: no endpoint changes any of this."""

    # Root of the per-round logs the Rust writes: <log_dir>/<tournament>/<round>.jsonl.
    log_dir: str = "logs"

    # One specific log to open by default. None means "the most recently written one under
    # log_dir", which is almost always the run you just started.
    log_path: str | None = None
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    # Origins the dashboard is served from in development.
    cors_origins: tuple[str, ...] = (
        "http://localhost:4321",
        "http://127.0.0.1:4321",
    )
