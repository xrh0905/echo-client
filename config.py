"""Configuration helpers for echo-client.

All runtime configuration is stored alongside the application's entry point,
making the deployment self-contained and portable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from rich.console import Console

CONFIG_FILENAME = "config.yaml"

DEFAULT_CONFIG: Dict[str, Any] = {
    "command_prefix": "/",
    "username": "/",
    "host": "127.0.0.1",
    "port": 3000,
    "typewriting": True,
    "autopause": False,
    "autopausestr": ",，.。;；:：!！",
    "autopausetime": 20,
}


def _config_path() -> Path:
    """Return the absolute path to the configuration file."""
    base_dir = Path(__file__).resolve().parent
    path = (base_dir / CONFIG_FILENAME).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_config(path: Path, config: Dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=True), encoding="utf-8")


def load_config(console: Optional[Console] = None) -> Dict[str, Any]:
    """Load the configuration from disk.

    If the file is missing, the default configuration is written to the local
    configuration directory. Missing keys are automatically populated to keep
    existing files forward compatible.
    """
    path = _config_path()
    data: Dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
        if console is not None:
            console.print(f"[green]从 {path} 加载了配置[/]")
    else:
        if console is not None:
            console.print(f"[yellow]未检测到配置，将在 {path} 创建一个默认文件[/]")

    config: Dict[str, Any] = DEFAULT_CONFIG.copy()
    config.update(data)

    if not path.exists() or data != config:
        _write_config(path, config)

    return config


def save_config(config: Dict[str, Any], console: Optional[Console] = None) -> None:
    """Persist the provided configuration to disk."""
    path = _config_path()
    _write_config(path, config)
    if console is not None:
        console.print(f"[green]配置已保存至 {path}[/]")


__all__ = [
    "DEFAULT_CONFIG",
    "load_config",
    "save_config",
]
