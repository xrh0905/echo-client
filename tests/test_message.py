from __future__ import annotations

import json

from echo_client.config import DEFAULT_CONFIG
from echo_client.message import (
    apply_autopause,
    format_username,
    get_delay,
    get_typewriting_string,
    parse_message,
    render,
)


def test_parse_message_supports_markdown_and_fast_formatting() -> None:
    parsed = parse_message("@b粗体 @r**强** *斜* `码` @[#66ccff]蓝 @{smile} @<warn>类")

    assert any(entry.get("style", {}).get("bold") for entry in parsed if entry.get("text") == "粗体 ")
    assert any(entry.get("style", {}).get("bold") for entry in parsed if entry.get("text") == "强")
    assert any(entry.get("style", {}).get("italic") for entry in parsed if entry.get("text") == "斜")
    assert any(entry.get("style", {}).get("code") for entry in parsed if entry.get("text") == "码")
    assert any(entry.get("emoji") == "smile" for entry in parsed)
    assert any("echo-text-warn" in entry.get("class", []) for entry in parsed)


def test_parse_message_supports_escaped_at_and_event_token() -> None:
    parsed = parse_message(r"\@字面 @sh喊")

    assert parsed[0]["text"].startswith("@字面")
    assert any(entry.get("event") == "shout" for entry in parsed)


def test_autopause_delay_and_render_payload() -> None:
    config = DEFAULT_CONFIG.copy()
    config.update(
        {
            "username": "测试",
            "username_brackets": True,
            "autopause": True,
            "autopausestr": "，",
            "autopausetime": 2,
            "typewriting": True,
            "print_speed": 5,
        }
    )

    messages = apply_autopause(config, parse_message("你好，世界"))
    payload = json.loads(render(config, messages))

    assert format_username(config) == "【测试】"
    assert any(entry.get("pause") == 2 for entry in messages)
    assert get_delay(config, messages) > 0
    assert payload["action"] == "message_data"
    rendered_entries = payload["data"]["messages"][0]["message"]
    assert all(entry.get("data", {}).get("printSpeed") == 5 for entry in rendered_entries if entry.get("text"))
    assert any("typewrite" in entry for entry in rendered_entries if entry.get("text"))


def test_typewriting_schemes() -> None:
    assert get_typewriting_string("凉宫", "pinyin") == "liang'gong"
    assert get_typewriting_string("凉宫", "zhuyin")
