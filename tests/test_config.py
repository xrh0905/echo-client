from __future__ import annotations

from echo_client.config import DEFAULT_CONFIG, normalize_config


def test_normalize_config_fills_defaults_and_migrates_auto_quotes() -> None:
    config = normalize_config(
        {
            "auto_quotes": False,
            "port": "3001",
            "print_speed": "12",
            "typewriting": "off",
            "skip_mode": "hide_display",
        }
    )

    assert config["quote_style"] == "none"
    assert config["port"] == 3001
    assert config["print_speed"] == 12
    assert config["typewriting"] is False
    assert config["skip_mode"] == "hide_display"
    assert config["username"] == DEFAULT_CONFIG["username"]


def test_normalize_config_rejects_invalid_values() -> None:
    config = normalize_config(
        {
            "command_prefix": "",
            "port": -1,
            "print_speed": "fast",
            "typewriting_scheme": "kana",
            "quote_style": "bad",
            "skip_mode": "unknown",
            "auto_suffix": "yes",
        }
    )

    assert config["command_prefix"] == DEFAULT_CONFIG["command_prefix"]
    assert config["port"] == DEFAULT_CONFIG["port"]
    assert config["print_speed"] == DEFAULT_CONFIG["print_speed"]
    assert config["typewriting_scheme"] == DEFAULT_CONFIG["typewriting_scheme"]
    assert config["quote_style"] == DEFAULT_CONFIG["quote_style"]
    assert config["skip_mode"] == DEFAULT_CONFIG["skip_mode"]
    assert config["auto_suffix"] is True
