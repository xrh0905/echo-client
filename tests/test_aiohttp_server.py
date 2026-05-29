from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

from aiohttp import ClientSession, WSMsgType
from rich.console import Console

from echo_client.protocol import BroadcastEnvelope, ClientType
from echo_client.server import EchoServer

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


async def start_server(tmp_path: Path) -> tuple[EchoServer, int]:
    server = EchoServer(Console(record=True, force_terminal=False))
    port = unused_port()
    server.config["host"] = "127.0.0.1"
    server.config["port"] = port
    await server._start_site("127.0.0.1", port)
    return server, port


def test_aiohttp_root_route_sends_initial_ping(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ECHO_CLIENT_CONFIG_DIR", str(tmp_path))

    async def scenario() -> None:
        server, port = await start_server(tmp_path)
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"http://127.0.0.1:{port}/")
                message = await websocket.receive(timeout=2)
                assert message.type is WSMsgType.TEXT
                assert json.loads(message.data)["action"] == "ping"
                await websocket.close()
                await wait_for(lambda: not server._connections.sessions)
        finally:
            await server._close_all_clients()
            await server._stop_site()

    asyncio.run(scenario())


def test_aiohttp_ws_alias_and_targeted_delivery(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ECHO_CLIENT_CONFIG_DIR", str(tmp_path))

    async def scenario() -> None:
        server, port = await start_server(tmp_path)
        try:
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"http://127.0.0.1:{port}/ws")
                await websocket.receive(timeout=2)
                await websocket.send_json(
                    {
                        "action": "hello",
                        "from": {"type": "history", "name": "history-panel"},
                        "data": {"hidden": False},
                    }
                )
                await wait_for(
                    lambda: bool(server._connections.sessions)
                    and server._connections.sessions[0].client_type is ClientType.HISTORY
                    and server._connections.sessions[0].name == "history-panel"
                )

                server._enqueue_payload(
                    BroadcastEnvelope("history_clear").to_json(),
                    label="history_clear",
                    target_types={ClientType.HISTORY},
                )
                message = await websocket.receive(timeout=2)
                assert json.loads(message.data)["action"] == "history_clear"
                await websocket.close()
                await wait_for(lambda: not server._connections.sessions)
        finally:
            await server._close_all_clients()
            await server._stop_site()

    asyncio.run(scenario())


def test_healthz_reports_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ECHO_CLIENT_CONFIG_DIR", str(tmp_path))

    async def scenario() -> None:
        server, port = await start_server(tmp_path)
        try:
            async with ClientSession() as client:
                response = await client.get(f"http://127.0.0.1:{port}/healthz")
                assert response.status == 200
                payload = await response.json()
                assert payload["status"] == "ok"
                assert payload["connections"] == 0
                assert payload["port"] == port
        finally:
            await server._close_all_clients()
            await server._stop_site()

    asyncio.run(scenario())


def test_no_legacy_ws_runtime_dependency() -> None:
    checked_paths = [
        Path("pyproject.toml"),
        Path("requirements.txt"),
        Path(".github/workflows/nuitka-build.yml"),
        Path("echo_client/server.py"),
        Path("echo_client/connection.py"),
    ]
    for path in checked_paths:
        assert "web" + "sockets" not in path.read_text(encoding="utf-8").lower()
