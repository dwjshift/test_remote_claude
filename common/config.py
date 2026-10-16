"""JSON configuration helpers shared by the client and server."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from .protocol import canonical_json


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "remote-claude" / "config.json"


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


def load_config(path: str | Path) -> dict:
    config_path = Path(path).expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError("The config must be a JSON object")
    remote = config.get("remote")
    if not isinstance(remote, str) or not remote.strip():
        raise ConfigError("remote must be a nonempty Git URL or path")
    if ":" not in remote:
        local_remote = Path(remote).expanduser()
        if not local_remote.is_absolute():
            local_remote = config_path.parent / local_remote
        config["remote"] = str(local_remote.resolve())
    config.setdefault("branch", "mailbox")
    config.setdefault("watch_interval", 10)
    if not isinstance(config["branch"], str) or not config["branch"]:
        raise ConfigError("branch must be a nonempty string")
    interval = config["watch_interval"]
    if (
        isinstance(interval, bool) or not isinstance(interval, (int, float))
        or not math.isfinite(interval) or interval <= 0
    ):
        raise ConfigError("watch_interval must be a positive finite number")
    state_value = config.get("state_dir", "state")
    if not isinstance(state_value, str) or not state_value.strip():
        raise ConfigError("state_dir must be a nonempty path string")
    state_dir = Path(state_value).expanduser()
    if not state_dir.is_absolute():
        state_dir = config_path.parent / state_dir
    config["state_dir"] = str(state_dir.resolve())
    return config


def save_config(path: str | Path, config: dict) -> None:
    if not isinstance(config, dict):
        raise ConfigError("The config must be a JSON object")
    config_path = Path(path).expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(config)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{config_path.name}.", dir=config_path.parent, delete=False
        ) as output:
            temporary = output.name
            os.chmod(temporary, 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, config_path)
    except OSError as exc:
        raise ConfigError(f"Cannot write config {config_path}: {exc}") from exc
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
