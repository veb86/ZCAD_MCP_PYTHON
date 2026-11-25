"""Configuration of the ZCAD MCP orchestrator.

All settings come from environment variables; an optional ``.env`` file next to
this module (or the file named by ``ZCAD_MCP_ENV_FILE``) is loaded first and never
overrides variables that are already set in the process environment.

Relative paths (``ZCAD_MCP_WORKSPACE``, ``ZCAD_MCP_SKILLS``) are resolved against
the project directory (the directory of this file), not against the current
working directory: MCP clients often start the server from an arbitrary cwd.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_DIR = Path(__file__).resolve().parent

DEFAULT_BRIDGE_URL = "http://127.0.0.1:5000"

log = logging.getLogger("zcad_mcp")


class ConfigError(ValueError):
    """Invalid configuration value."""


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Build with :func:`load_settings`."""

    bridge_url: str = DEFAULT_BRIDGE_URL
    workspace_dir: Path = PROJECT_DIR / "workspace"
    skills_dir: Path = PROJECT_DIR / "skills"
    max_segments: int = 200
    skill_timeout_sec: float = 60.0
    # Hard limit for the serialized skill result returned to the model.
    max_result_bytes: int = 200_000
    # Address-space limit for the skill worker process (POSIX only, 0 = off).
    skill_memory_mb: int = 2048
    # ZCAD HTTPServer waits up to 30 s for the GUI thread; leave headroom.
    bridge_timeout_sec: float = 35.0
    bridge_token: str | None = None
    # Bridge command used by zcad_export_selection (args: [absolute_dxf_path]).
    export_command: str = "EXPORT"
    export_wait_sec: float = 30.0
    # "batch" = BEGIN_BATCH/BATCH_LINES/END_BATCH (one undo step), "line" = LINE per segment.
    draw_mode: str = "batch"
    batch_chunk_size: int = 500
    max_draw_lines: int = 10_000
    log_level: str = "INFO"
    current_dxf_name: str = "selection.dxf"

    @property
    def default_dxf_path(self) -> Path:
        return self.workspace_dir / self.current_dxf_name

    def ensure_dirs(self) -> None:
        """Create ``workspace/`` and ``skills/`` if they do not exist."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)


def _load_env_file(env: dict[str, str]) -> None:
    path_value = env.get("ZCAD_MCP_ENV_FILE")
    path = Path(path_value) if path_value else PROJECT_DIR / ".env"
    if not path.is_file():
        return
    try:
        from dotenv import dotenv_values  # python-dotenv ships with mcp[cli]
    except ImportError:  # pragma: no cover - fallback parser
        values: dict[str, str | None] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    else:
        values = dict(dotenv_values(path))
    for key, value in values.items():
        if value is not None and key not in env:
            env[key] = value


def _resolve_dir(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not value > 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


def load_settings(environ: Mapping[str, str] | None = None, *, load_dotenv: bool = True) -> Settings:
    """Build :class:`Settings` from ``environ`` (default: ``os.environ``) and ``.env``."""
    env = dict(os.environ if environ is None else environ)
    if load_dotenv:
        _load_env_file(env)

    bridge_url = (env.get("ZCAD_BRIDGE_URL") or DEFAULT_BRIDGE_URL).strip().rstrip("/")
    if not bridge_url.startswith(("http://", "https://")):
        raise ConfigError(f"ZCAD_BRIDGE_URL must start with http:// or https://, got {bridge_url!r}")

    draw_mode = (env.get("ZCAD_DRAW_MODE") or "batch").strip().lower()
    if draw_mode not in ("batch", "line"):
        raise ConfigError(f"ZCAD_DRAW_MODE must be 'batch' or 'line', got {draw_mode!r}")

    export_command = (env.get("ZCAD_EXPORT_COMMAND") or "EXPORT").strip()
    if not export_command:
        raise ConfigError("ZCAD_EXPORT_COMMAND must not be empty")

    return Settings(
        bridge_url=bridge_url,
        workspace_dir=_resolve_dir(env.get("ZCAD_MCP_WORKSPACE"), PROJECT_DIR / "workspace"),
        skills_dir=_resolve_dir(env.get("ZCAD_MCP_SKILLS"), PROJECT_DIR / "skills"),
        max_segments=_int(env, "ZCAD_MCP_MAX_SEGMENTS", 200, minimum=1),
        skill_timeout_sec=_float(env, "ZCAD_MCP_SKILL_TIMEOUT_SEC", 60.0),
        max_result_bytes=_int(env, "ZCAD_MCP_MAX_RESULT_BYTES", 200_000, minimum=1024),
        skill_memory_mb=_int(env, "ZCAD_MCP_SKILL_MEMORY_MB", 2048, minimum=0),
        bridge_timeout_sec=_float(env, "ZCAD_BRIDGE_TIMEOUT_SEC", 35.0),
        bridge_token=env.get("ZCAD_BRIDGE_TOKEN") or None,
        export_command=export_command,
        export_wait_sec=_float(env, "ZCAD_EXPORT_WAIT_SEC", 30.0),
        draw_mode=draw_mode,
        batch_chunk_size=_int(env, "ZCAD_BATCH_CHUNK_SIZE", 500, minimum=1),
        max_draw_lines=_int(env, "ZCAD_MCP_MAX_DRAW_LINES", 10_000, minimum=1),
        log_level=(env.get("ZCAD_MCP_LOG_LEVEL") or "INFO").strip().upper(),
    )


def setup_logging(level: str = "INFO") -> None:
    """Send all logs to stderr: stdout is reserved for the MCP stdio transport."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("zcad_mcp")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    # httpx logs every request at INFO; Bridge errors are logged by zcad_mcp itself.
    logging.getLogger("httpx").setLevel(logging.WARNING)
