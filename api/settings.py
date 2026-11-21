"""What the server was started with. Set once at launch, never from a request."""

from __future__ import annotations

from dataclasses import dataclass, field

from analysis.config_defaults import PipelineConfig


@dataclass(frozen=True)
class Settings:
    """Read-only by construction: no endpoint changes any of this."""

    log_path: str = "logs/run.jsonl"
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    # Origins the dashboard is served from in development.
    cors_origins: tuple[str, ...] = (
        "http://localhost:4321",
        "http://127.0.0.1:4321",
    )
