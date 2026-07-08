"""Connection and event queue management for websocket clients."""
from __future__ import annotations

import asyncio
from itertools import count
from typing import Protocol
from typing import Any

from .protocol import ClientSession, ClientType, OutgoingEvent, effective_client_type


class WebSocketAdapter(Protocol):
    """Small transport interface used by the connection manager."""

    @property
    def closed(self) -> bool:
        """Return whether the underlying websocket is closed."""

    async def send(self, payload: str) -> None:
        """Send a text payload."""

    async def close(self, code: int, reason: str) -> None:
        """Close the websocket."""


class ConnectionManager:
    """Tracks Echo-Live websocket sessions and routes queued events."""

    def __init__(self) -> None:
        self._client_ids = count(1)
        self._sessions: dict[int, ClientSession] = {}

    @property
    def sessions(self) -> tuple[ClientSession, ...]:
        return tuple(self._sessions.values())

    def register(self, websocket: WebSocketAdapter) -> ClientSession:
        client_id = next(self._client_ids)
        session = ClientSession(
            client_id=client_id,
            websocket=websocket,
            queue=asyncio.Queue(),
            name=f"客户端{client_id}",
            client_type=ClientType.LIVE,
        )
        self._sessions[client_id] = session
        return session

    def unregister(self, client_id: int) -> ClientSession | None:
        return self._sessions.pop(client_id, None)

    def get(self, client_id: int) -> ClientSession | None:
        return self._sessions.get(client_id)

    def set_client_type(self, client_id: int, client_type: Any) -> ClientType:
        session = self._sessions.get(client_id)
        effective = effective_client_type(client_type)
        if session is not None:
            session.client_type = effective
        return effective

    def set_client_name(self, client_id: int, name: str | None) -> None:
        if not name:
            return
        session = self._sessions.get(client_id)
        if session is not None:
            session.name = name

    def enqueue(self, event: OutgoingEvent) -> None:
        for session in self._sessions.values():
            if event.target_types and effective_client_type(session.client_type) not in event.target_types:
                continue
            session.queue.put_nowait(event)

    def group_summary(self) -> str:
        groups = {
            ClientType.LIVE: [],
            ClientType.HISTORY: [],
            ClientType.SERVER: [],
        }
        for session in self._sessions.values():
            groups[effective_client_type(session.client_type)].append(str(session.client_id))

        segments: list[str] = []
        if groups[ClientType.LIVE]:
            segments.append("live: " + ",".join(groups[ClientType.LIVE]))
        if groups[ClientType.HISTORY]:
            segments.append("history: " + ",".join(groups[ClientType.HISTORY]))
        if groups[ClientType.SERVER]:
            segments.append("server: " + ",".join(groups[ClientType.SERVER]))
        return " | ".join(segments)

    async def close_all(self) -> None:
        async def _close_single(session: ClientSession) -> None:
            websocket = session.websocket
            if connection_is_closed(websocket):
                return
            try:
                await websocket.close(code=1001, reason="Server shutdown")
            except Exception:
                return

        sessions = list(self._sessions.values())
        if sessions:
            await asyncio.gather(*[_close_single(session) for session in sessions], return_exceptions=True)
        self._sessions.clear()


def connection_is_closed(websocket: Any) -> bool:
    closed = getattr(websocket, "closed", None)
    if isinstance(closed, bool):
        return closed

    closed_attr = getattr(websocket, "closed", None)
    if isinstance(closed_attr, bool):
        return closed_attr

    state = getattr(websocket, "state", None)
    if state is not None and str(state).upper().endswith("CLOSED"):
        return True

    return False


__all__ = ["ConnectionManager", "WebSocketAdapter", "connection_is_closed"]
