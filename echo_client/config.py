"""Configuration helpers for echo-client."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Optional

import yaml
from rich.console import Console

CONFIG_FILENAME = "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "command_prefix": "/",
    "username": "Someone",
    "host": "127.0.0.1",
    "port": 3000,
    "typewriting": True,
    "typewriting_scheme": "pinyin",
    "autopause": False,
    "autopausestr": ",，.。;；:：!！",
    "autopausetime": 10,
    "print_speed": 10,
    "quote_style": "en",
    "quote_custom_left": "",
    "quote_custom_right": "",
    "auto_parentheses": False,
    "username_brackets": True,
    "inhibit_ctrl_c": True,
    "auto_suffix": False,
    "auto_suffix_value": "喵",
    "skip_mode": "blank_text",
}

QUOTE_STYLES = {"en", "cn", "jp", "custom", "none"}
SKIP_MODES = {"echo_next", "blank_text", "hide_display"}
TYPEWRITING_SCHEMES = {"pinyin", "zhuyin"}

_BOOL_KEYS = {
    "typewriting",
    "autopause",
    "auto_parentheses",
    "username_brackets",
    "auto_suffix",
    "inhibit_ctrl_c",
}

_STRING_KEYS = {
    "command_prefix",
    "username",
    "host",
    "typewriting_scheme",
    "autopausestr",
    "quote_style",
    "quote_custom_left",
    "quote_custom_right",
    "auto_suffix_value",
    "skip_mode",
}

_POSITIVE_INT_KEYS = {
    "port",
    "autopausetime",
    "print_speed",
}


def _base_directory() -> Path:
    """Resolve the directory that should contain runtime configuration."""
    configured_dir = os.environ.get("ECHO_CLIENT_CONFIG_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser().resolve()

    if getattr(sys, "frozen", False):
        # Running inside a bundled executable (e.g. PyInstaller)
        return Path(sys.executable).resolve().parent
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0:
        try:
            return argv0.resolve().parent
        except OSError:
            pass
    return Path(__file__).resolve().parent


def _config_path() -> Path:
    """Return the absolute path to the configuration file."""
    base_dir = _base_directory()
    path = (base_dir / CONFIG_FILENAME).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "on", "1"}:
            return True
        if normalized in {"false", "no", "n", "off", "0"}:
            return False
    return default


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _coerce_string(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


def normalize_config(data: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete, validated runtime configuration."""
    raw = dict(data or {})
    config = DEFAULT_CONFIG.copy()

    legacy_auto_quotes = raw.get("auto_quotes")
    if legacy_auto_quotes is not None and "quote_style" not in raw:
        raw["quote_style"] = "en" if _coerce_bool(legacy_auto_quotes, True) else "none"

    for key in _BOOL_KEYS:
        if key in raw:
            config[key] = _coerce_bool(raw.get(key), bool(DEFAULT_CONFIG[key]))

    for key in _STRING_KEYS:
        if key in raw:
            config[key] = _coerce_string(raw.get(key), str(DEFAULT_CONFIG[key]))

    for key in _POSITIVE_INT_KEYS:
        if key in raw:
            config[key] = _coerce_positive_int(raw.get(key), int(DEFAULT_CONFIG[key]))

    if not config["command_prefix"]:
        config["command_prefix"] = DEFAULT_CONFIG["command_prefix"]

    typewriting_scheme = str(config["typewriting_scheme"]).strip().lower()
    config["typewriting_scheme"] = (
        typewriting_scheme if typewriting_scheme in TYPEWRITING_SCHEMES else DEFAULT_CONFIG["typewriting_scheme"]
    )

    quote_style = str(config["quote_style"]).strip().lower()
    config["quote_style"] = quote_style if quote_style in QUOTE_STYLES else DEFAULT_CONFIG["quote_style"]

    skip_mode = str(config["skip_mode"]).strip().lower()
    config["skip_mode"] = skip_mode if skip_mode in SKIP_MODES else DEFAULT_CONFIG["skip_mode"]

    return config


def _write_config(path: Path, config: dict[str, Any]) -> None:
    payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def load_config(console: Optional[Console] = None) -> dict[str, Any]:
    """Load the configuration from disk.

    If the file is missing, the default configuration is written to the local
    configuration directory. Missing keys are automatically populated to keep
    existing files forward compatible.
    """
    path = _config_path()
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
        elif loaded is not None and console is not None:
            console.print(f"[yellow]配置文件 {path} 格式无效，将使用默认配置覆盖[/]")
        if console is not None:
            console.print(f"[green]从 {path} 加载了配置[/]")
    else:
        if console is not None:
            console.print(f"[yellow]未检测到配置，将在 {path} 创建一个默认文件[/]")

    config = normalize_config(data)

    if not path.exists() or data != config:
        _write_config(path, config)

    return config


def save_config(config: dict[str, Any], console: Optional[Console] = None) -> None:
    """Persist the provided configuration to disk."""
    path = _config_path()
    _write_config(path, normalize_config(config))
    if console is not None:
        console.print(f"[green]配置已保存至 {path}[/]")


__all__ = [
    "DEFAULT_CONFIG",
    "load_config",
    "normalize_config",
    "save_config",
]
