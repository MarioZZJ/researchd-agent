"""Service configuration (pydantic-settings).

Priority: CLI flags > env vars > config file > defaults. Secrets never logged;
only key names are exposed for diagnostics.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = ".data"


class ApiConfig(BaseModel):
    """Internal API transport. UDS is preferred (IMPLEMENTATION.md §18).

    transport: "uds" (default) | "tcp" (localhost + bearer token).
    socket_path == "" means "derive from data_dir/run/researchd.sock" when
    transport is uds.
    """

    transport: str = "uds"
    socket_path: str = ""
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 8777
    token: str = ""  # required only for TCP fallback


class InteractionConfig(BaseModel):
    """researchd acp interaction model (§15.3): session-level only, never
    changes the project execution policy."""

    default_profile: str = "frontdesk_fast"
    deterministic_commands: bool = True
    allow_session_override: bool = True
    allow_natural_language_intent: bool = True
    intent_confidence_threshold: float = 0.85


class SchedulerConfig(BaseModel):
    """Scheduler tuning (IMPLEMENTATION.md §14)."""

    executor: str = "fake"  # fake | reasonix | codex
    delivery: str = "fake"  # fake | cc_connect
    max_parallel: int = 4
    tick_seconds: float = 2.0


class ProfileConfig(BaseModel):
    """Named executor profile (IMPLEMENTATION.md §15.1): resolved model and
    reasoning effort. Profile names are referenced by Task contracts and by
    project role overrides."""

    model: str | None = None
    reasoning_effort: str | None = None
    process_instance_id: str | None = None


DEFAULT_PROFILES: dict[str, ProfileConfig] = {
    "reasonix_worker": ProfileConfig(model="gateway/deepseek-v4-flash", reasoning_effort="medium"),
    "reasonix_planner": ProfileConfig(model="gateway/gpt-5.6-sol", reasoning_effort="high"),
    "reasonix_auditor": ProfileConfig(model="gateway/gpt-5.6-sol", reasoning_effort="high"),
    "reasonix_literature": ProfileConfig(model="gateway/deepseek-v4-flash", reasoning_effort="medium"),
    "fake_worker": ProfileConfig(),
    "fake_planner": ProfileConfig(),
    "fake_auditor": ProfileConfig(),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESEARCHD_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_dir: str = Field(default=DEFAULT_DATA_DIR, validation_alias=AliasChoices("RESEARCHD_DATA_DIR", "data_dir"))
    db_path: str = Field(default=".data/researchd.db", validation_alias=AliasChoices("RESEARCHD_DB", "db_path"))
    log_level: str = "info"
    api: ApiConfig = Field(default_factory=ApiConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=lambda: dict(DEFAULT_PROFILES))
    service_name: str = "researchd"

    def resolve(self, repo_root: Path | None = None) -> "Settings":
        """Resolve relative paths against the repo root (cwd is the service cwd)."""
        base = repo_root or Path.cwd()
        if not Path(self.data_dir).is_absolute():
            self.data_dir = str(base / self.data_dir)
        if not Path(self.db_path).is_absolute():
            self.db_path = str(base / self.db_path)
        api = self.api.model_copy()
        if api.transport not in ("uds", "tcp"):
            raise ValueError(f"api.transport must be 'uds' or 'tcp', got {api.transport!r}")
        if api.transport == "tcp":
            api.socket_path = ""  # TCP transport never uses the socket
        elif not api.socket_path:
            # derive the UDS socket from the data dir
            api.socket_path = str(Path(self.data_dir) / "run" / "researchd.sock")
        elif not Path(api.socket_path).is_absolute():
            api.socket_path = str(base / api.socket_path)
        self.api = api
        return self

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, Path(self.api.socket_path).parent, Path(self.db_path).parent):
            Path(d).mkdir(parents=True, exist_ok=True)


def default_settings() -> Settings:
    return Settings().resolve()


def load_settings_file(path: str | None) -> Settings:
    """Load a settings file (simple key=value) with precedence:
    CLI > env > file > defaults. File values are only applied when the
    corresponding RESEARCHD_* env var is absent. Caller resolves paths."""
    s = Settings()
    if not (path and Path(path).exists()):
        return s
    import os

    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        env_key = "RESEARCHD_" + key.upper().replace(".", "__")
        if os.environ.get(env_key) is not None:
            continue  # env wins over file
        if key.startswith("api."):
            setattr(s.api, key.split(".", 1)[1], value)
        elif key.startswith("interaction."):
            setattr(s.interaction, key.split(".", 1)[1], value)
        else:
            setattr(s, key, value)
    return s


def env_snapshot() -> dict:
    """Diagnostics-only: key names + presence, never values."""
    return {
        "RESEARCHD_DATA_DIR": bool(os.environ.get("RESEARCHD_DATA_DIR")),
        "RESEARCHD_DB": bool(os.environ.get("RESEARCHD_DB")),
        "RESEARCHD_API__TOKEN": bool(os.environ.get("RESEARCHD_API__TOKEN")),
    }
