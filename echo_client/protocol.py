"""Typed protocol helpers for Echo-Live websocket traffic."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Any, Iterable


class ClientType(StrEnum):
    """Known Echo-Live client roles."""

    HISTORY = "history"
    LIVE = "live"
    SERVER = "server"
    UNKNOWN = "unknown"


class SkipMode(StrEnum):
    """Supported skip command behaviors."""

    ECHO_NEXT = "echo_next"
    BLANK_TEXT = "blank_text"
    HIDE_DISPLAY = "hide_display"


class QuoteStyle(StrEnum):
    """Supported automatic quote styles."""

    EN = "en"
    CN = "cn"
    JP = "jp"
    CUSTOM = "custom"
    NONE = "none"


@dataclass(frozen=True)
class BroadcastEnvelope:
    """Echo-Live broadcast API envelope."""

    action: str
    data: dict[str, Any] = field(default_factory=dict)
    from_: dict[str, Any] | None = None
    target: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action, "data": self.data}
        if self.from_:
            payload["from"] = self.from_
        if self.target:
            payload["target"] = self.target
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(frozen=True)
class OutgoingEvent:
    """Payload queued for one or more websocket clients."""

    payload: str
    label: str | None = None
    description: str | None = None
    delay: int | float | None = None
    target_types: frozenset[ClientType] = field(default_factory=frozenset)

    @classmethod
    def from_envelope(
        cls,
        envelope: BroadcastEnvelope,
        *,
        label: str | None = None,
        description: str | None = None,
        delay: int | float | None = None,
        target_types: Iterable[str | ClientType] | None = None,
    ) -> "OutgoingEvent":
        return cls(
            payload=envelope.to_json(),
            label=label or envelope.action,
            description=description,
            delay=delay,
            target_types=normalize_target_types(target_types),
        )


@dataclass
class ClientSession:
    """Runtime state for a connected Echo-Live websocket client."""

    client_id: int
    websocket: Any
    queue: Any
    name: str
    client_type: ClientType = ClientType.LIVE
    heartbeat_count: int = 0
    live_display_visible: bool = False
    graceful_disconnect_requested: bool = False


PING_PAYLOAD = BroadcastEnvelope("ping").to_json()


def normalize_client_type(value: Any) -> ClientType:
    if not isinstance(value, str):
        return ClientType.UNKNOWN
    normalized = value.strip().lower()
    try:
        return ClientType(normalized)
    except ValueError:
        return ClientType.UNKNOWN


def effective_client_type(value: Any) -> ClientType:
    normalized = normalize_client_type(value)
    return ClientType.LIVE if normalized is ClientType.UNKNOWN else normalized


def normalize_skip_mode(value: Any) -> SkipMode:
    if not isinstance(value, str):
        return SkipMode.BLANK_TEXT
    normalized = value.strip().lower()
    try:
        return SkipMode(normalized)
    except ValueError:
        return SkipMode.BLANK_TEXT


def normalize_target_types(values: Iterable[str | ClientType] | None) -> frozenset[ClientType]:
    if values is None:
        return frozenset()
    normalized = {
        effective_client_type(str(value))
        for value in values
        if isinstance(value, (str, ClientType)) and str(value)
    }
    normalized.discard(ClientType.UNKNOWN)
    return frozenset(normalized)


def parse_envelope(raw_message: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "BroadcastEnvelope",
    "ClientSession",
    "ClientType",
    "OutgoingEvent",
    "PING_PAYLOAD",
    "QuoteStyle",
    "SkipMode",
    "effective_client_type",
    "normalize_client_type",
    "normalize_skip_mode",
    "normalize_target_types",
    "parse_envelope",
]
