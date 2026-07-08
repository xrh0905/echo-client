from __future__ import annotations

import json

from echo_client.protocol import (
    BroadcastEnvelope,
    ClientType,
    SkipMode,
    normalize_skip_mode,
    normalize_target_types,
    parse_envelope,
)


def test_broadcast_envelope_serializes_official_shape() -> None:
    payload = BroadcastEnvelope(
        "message_data",
        {"username": "Someone", "messages": []},
        from_={"type": "server", "name": "echo-client"},
        target={"name": "live"},
    ).to_json()

    parsed = json.loads(payload)
    assert parsed["action"] == "message_data"
    assert parsed["data"]["username"] == "Someone"
    assert parsed["from"]["type"] == "server"
    assert parsed["target"]["name"] == "live"


def test_parse_envelope_rejects_non_object_json() -> None:
    assert parse_envelope("[1, 2]") is None
    assert parse_envelope("{bad") is None


def test_normalizers() -> None:
    assert normalize_skip_mode("echo_next") is SkipMode.ECHO_NEXT
    assert normalize_skip_mode("bad") is SkipMode.BLANK_TEXT
    assert normalize_target_types(["live", "history", "bad"]) == frozenset(
        {ClientType.LIVE, ClientType.HISTORY}
    )
