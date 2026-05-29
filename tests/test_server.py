from __future__ import annotations

from rich.console import Console

from echo_client.server import EchoServer


def make_server(monkeypatch, tmp_path) -> EchoServer:
    monkeypatch.setenv("ECHO_CLIENT_CONFIG_DIR", str(tmp_path))
    return EchoServer(Console(record=True, force_terminal=False))


def test_literal_message_from_repeated_prefix() -> None:
    assert EchoServer._literal_message_from_command("//hello", "/") == "/hello"
    assert EchoServer._literal_message_from_command("///hello", "/") == "//hello"
    assert EchoServer._literal_message_from_command("/hello", "/") is None


def test_commands_enqueue_skip_and_clear_without_clients(monkeypatch, tmp_path) -> None:
    server = make_server(monkeypatch, tmp_path)
    server.config["skip_mode"] = "echo_next"

    assert server._handle_console_command("/skip") is True
    assert server._handle_console_command("/clear") is True


def test_command_catalog_alias_and_argument_validation(monkeypatch, tmp_path) -> None:
    server = make_server(monkeypatch, tmp_path)

    assert server._handle_console_command("/ps nope") is True
    assert server._handle_console_command("/tt") is True
    assert server.config["typewriting"] is False


def test_message_decoration_suffix_and_quotes(monkeypatch, tmp_path) -> None:
    server = make_server(monkeypatch, tmp_path)
    server.config.update({"auto_suffix": True, "auto_suffix_value": "喵", "quote_style": "en"})

    assert server._decorate_outgoing_text(server._apply_auto_suffix("你好")) == '"你好喵"'
