"""OSC sender for VRChat Chatbox integration."""
from __future__ import annotations

import logging
from typing import Any
from pythonosc.udp_client import SimpleUDPClient

_logger = logging.getLogger(__name__)


def _get_simple_udp_client(host: str, port: int) -> Any:
    """Create a SimpleUDPClient for the given address."""

    return SimpleUDPClient(host, port)


def send_vrchat_chatbox(host: str, port: int, text: str) -> None:
    """Send a plain-text message to VRChat Chatbox via OSC.

    Uses the ``/chatbox/input`` OSC address with parameters ``(text, True)``
    to display the message immediately.  Network errors are logged but never
    raised so that a single failed OSC delivery does not interrupt the main
    console workflow.
    """
    try:
        client = _get_simple_udp_client(host, port)
        client.send_message("/chatbox/input", [text, True])
    except OSError:
        _logger.warning("无法连接到 OSC 服务器 %s:%d，消息未发送。", host, port, exc_info=True)
    except Exception:  # pragma: no cover - defensive
        _logger.warning("发送 OSC 消息时出现未预期的错误。", exc_info=True)


def extract_plain_text(parsed_message: list[dict[str, Any]]) -> str:
    """Extract plain text from a parsed Echo-live message structure.

    Walks the list of message segments produced by :func:`~echo_client.message.parse_message`
    and concatenates all text fragments, discarding style information, emoji
    markers, CSS classes, and typewriting data.  The result is a clean
    plain-text string suitable for display in VRChat Chatbox.
    """
    parts: list[str] = []
    for segment in parsed_message:
        text = segment.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


__all__ = [
    "extract_plain_text",
    "send_vrchat_chatbox",
]
