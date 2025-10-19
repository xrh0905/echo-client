"""WebUI HTTP and WebSocket server implementation."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence, Set

from aiohttp import web, WSMsgType
from websockets import exceptions as ws_exceptions
from websockets.frames import Close

ControlCallback = Callable[[Dict[str, Any], str], None]

logger = logging.getLogger(__name__)


@dataclass
class _WebSocketState:
    """Adapts aiohttp state to websockets' naming for compatibility."""

    websocket: web.WebSocketResponse

    @property
    def name(self) -> str:
        if self.websocket.closed:
            return "CLOSED"
        if self.websocket.close_code is not None:
            return "CLOSING"
        return "OPEN"


class AioHTTPWebSocketAdapter:
    """Minimal adapter so EchoServer can keep using websockets idioms."""

    def __init__(self, websocket: web.WebSocketResponse) -> None:
        self._ws = websocket
        self.state = _WebSocketState(self._ws)

    def __aiter__(self) -> "AioHTTPWebSocketAdapter":
        return self

    async def __anext__(self) -> Any:
        msg = await self._ws.receive()
        if msg.type == WSMsgType.TEXT:
            return msg.data
        if msg.type == WSMsgType.BINARY:
            return msg.data
        if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}:
            raise StopAsyncIteration
        if msg.type == WSMsgType.ERROR:
            close = Close(self._ws.close_code or 1011, "websocket connection error")
            raise ws_exceptions.ConnectionClosedError(close, close)
        # Skip pings/pongs/continuation frames transparently
        return await self.__anext__()

    async def send(self, data: str) -> None:
        if self._ws.closed:
            code = self._ws.close_code or 1000
            raw_reason = getattr(self._ws, "close_message", "") or "connection already closed"
            if isinstance(raw_reason, bytes):
                reason = raw_reason.decode("utf-8", errors="ignore")
            else:
                reason = str(raw_reason)
            close = Close(code, reason)
            raise ws_exceptions.ConnectionClosedOK(close, close)
        await self._ws.send_str(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code, message=reason.encode("utf-8"))

    @property
    def closed(self) -> bool:
        return self._ws.closed


class WebUIServer:
    """HTTP and WebSocket server for the echo-client WebUI."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 3000,
        webui_root: str = "echoliveui",
        save_endpoint: str = "/api/save",
        websocket_path: str = "/ws",
        modules: Optional[Sequence[str]] = None,
        config_context: Optional[Dict[str, Any]] = None,
        control_callback: Optional[ControlCallback] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.webui_root = Path(webui_root)
        self.save_endpoint = save_endpoint
        self.websocket_path = websocket_path.rstrip("/") or "/ws"
        self.modules = list(modules) if modules else ["echo_webui"]
        self.config_context: Dict[str, Any] = config_context or {}

        self._client_ws_handler: Optional[Callable[[Any], Awaitable[None]]] = None
        self._http_app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._channel_map: Dict[str, Set[web.WebSocketResponse]] = {}
        self._root_connections: Set[web.WebSocketResponse] = set()
        self._shutdown_event = asyncio.Event()
        self._control_callback = control_callback

    async def start(self, client_ws_handler: Callable[[Any], Awaitable[None]]) -> None:
        """Start the WebUI server and register the primary websocket handler."""

        self._client_ws_handler = client_ws_handler
        self._shutdown_event.clear()

        app = web.Application()
        app.on_shutdown.append(self._gracefully_close_channels)
        self._http_app = app
        self._setup_routes(app)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        logger.info("WebUI server started at http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the WebUI server."""
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

        await self._gracefully_close_channels(self._http_app)
        self._shutdown_event.set()
        logger.info("WebUI server stopped")

    async def wait_closed(self) -> None:
        """Wait until the WebUI server has been stopped."""
        await self._shutdown_event.wait()

    def _setup_routes(self, app: web.Application) -> None:
        """Configure HTTP and WebSocket routes."""

        app.router.add_get("/", self._handle_root)
        app.router.add_post(self.save_endpoint, self._handle_save_config)
        app.router.add_get(f"{self.websocket_path}{{tail:.*}}", self._handle_live_channel)

        if self.webui_root.exists():
            app.router.add_static("/", self.webui_root, show_index=False)
        else:
            logger.warning("WebUI root '%s' does not exist; static assets won't be served", self.webui_root)

    async def _handle_root(self, request: web.Request) -> web.StreamResponse:
        """Serve index page or upgrade to websocket depending on headers."""

        ws = web.WebSocketResponse()
        can_prepare = ws.can_prepare(request)
        if can_prepare.ok:
            await ws.prepare(request)
            if self._client_ws_handler is None:
                await ws.close(code=1011, message=b"Websocket handler missing")
                return ws

            adapter = AioHTTPWebSocketAdapter(ws)
            self._root_connections.add(ws)
            try:
                await self._client_ws_handler(adapter)
            finally:
                self._root_connections.discard(ws)
                if not ws.closed:
                    with contextlib.suppress(Exception):
                        await ws.close()
            return ws

        return await self._serve_index()

    async def _serve_index(self) -> web.StreamResponse:
        index_file = self.webui_root / "editor.html"
        if index_file.exists():
            return web.FileResponse(index_file)
        return web.Response(text="WebUI not found", status=404)

    async def _handle_save_config(self, request: web.Request) -> web.Response:
        """Handle POST requests to save configuration files."""

        try:
            data = await request.json()
        except json.JSONDecodeError:
            logger.debug("Invalid JSON in request body")
            return web.json_response({"error": "保存配置失败: 无效的 JSON 数据"}, status=400)

        logger.debug("处理API请求: %s %s", request.method, request.path)
        logger.debug("请求头: %s", dict(request.headers))
        logger.debug("请求体内容: %s", data)

        name = data.get("name")
        root = data.get("root")
        config_data = data.get("data")

        if not name or not root or not config_data:
            error_msg = "保存配置失败: 请求体缺少必要字段"
            logger.debug("验证失败: %s", error_msg)
            return web.json_response({"error": error_msg}, status=400)

        try:
            root_path = Path(root).resolve()
            file_path = (root_path / name).resolve()

            try:
                file_path.relative_to(root_path)
            except ValueError:
                error_msg = "保存配置失败: 无效的文件路径"
                logger.warning("Path traversal attempt detected: %s not in %s", file_path, root_path)
                return web.json_response({"error": error_msg}, status=400)

            if any(sep in name for sep in ("/", "\\")) or ".." in name:
                error_msg = "保存配置失败: 文件名包含非法字符"
                logger.warning("Invalid filename: %s", name)
                return web.json_response({"error": error_msg}, status=400)

            if not root_path.exists():
                logger.debug("目录不存在，创建目录: %s", root_path)
                root_path.mkdir(parents=True, exist_ok=True)

            logger.debug("准备写入文件: %s", file_path)
            file_path.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.debug("文件写入成功: %s", file_path)

            response_data = {
                "success": True,
                "message": "配置文件保存成功",
                "path": str(file_path),
            }
            logger.debug("发送成功响应: %s", response_data)
            return web.json_response(response_data)

        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.debug("保存配置时捕获到错误: %s", error)
            logger.exception("Error details:")
            return web.json_response({"error": str(error)}, status=500)

    async def _handle_live_channel(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebUI websocket connections with channel support."""

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        tail = request.match_info.get("tail", "")
        normalized_tail = tail.strip("/")
        channel_name = normalized_tail.split("/", 1)[0] if normalized_tail else "global"

        logger.info("新的WebSocket连接，客户端地址: %s, 频道: %s", request.remote, channel_name)

        bucket = self._channel_map.setdefault(channel_name, set())
        bucket.add(ws)

        ping_task = asyncio.create_task(self._ping_client(ws, channel_name))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_channel_message(msg.data, ws, channel_name)
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket错误，频道: %s: %s", channel_name, ws.exception())
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ping_task

            logger.info("WebSocket客户端断开连接，频道: %s", channel_name)
            bucket.discard(ws)
            if not bucket:
                self._channel_map.pop(channel_name, None)

        return ws

    async def _ping_client(self, ws: web.WebSocketResponse, channel_name: str) -> None:
        """Send periodic pings to keep channel connections alive."""

        try:
            while not ws.closed:
                await asyncio.sleep(30)
                if not ws.closed:
                    await ws.ping()
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Ping error for channel %s: %s", channel_name, exc)

    async def _handle_channel_message(
        self,
        message: str,
        ws: web.WebSocketResponse,
        channel_name: str,
    ) -> None:
        """Process incoming WebUI websocket message and broadcast appropriately."""

        try:
            parsed_msg = json.loads(message)
            logger.debug("收到WebSocket消息，频道: %s, 消息: %s", channel_name, message)

            if not parsed_msg or "from" not in parsed_msg or "data" not in parsed_msg:
                logger.warning("收到格式不正确的消息: %s", message)
                return

            from_info = parsed_msg.get("from", {})
            action = parsed_msg.get("action")
            target = parsed_msg.get("target")

            from_type = from_info.get("type")
            from_uuid = from_info.get("uuid")
            if self._control_callback is not None:
                try:
                    self._control_callback(parsed_msg, channel_name)
                except Exception as callback_error:  # pylint: disable=broad-exception-caught
                    logger.error("Control callback failed: %s", callback_error, exc_info=True)


            if from_type == "live":
                if action == "hello" and target is None:
                    logger.info("对话框加入服务器, UUID: %s, 频道: %s", from_uuid, channel_name)
                elif action == "close" and target is None:
                    logger.info("对话框离开服务器, UUID: %s, 频道: %s", from_uuid, channel_name)
            elif from_type == "history":
                if action == "hello" and target is None:
                    logger.info("历史记录浏览器加入服务器, UUID: %s, 频道: %s", from_uuid, channel_name)
                elif action == "close" and target is None:
                    logger.info("历史记录浏览器离开服务器, UUID: %s, 频道: %s", from_uuid, channel_name)
            elif from_type == "server":
                if action == "ping" and target is None:
                    logger.info("编辑器加入服务器, UUID: %s, 频道: %s", from_uuid, channel_name)
            else:
                logger.warning("收到未定义类型的消息: %s, 频道: %s, 原始: %s", from_type, channel_name, message)

            await self._broadcast_channel_message(parsed_msg, ws, channel_name)

        except json.JSONDecodeError:
            logger.error("Failed to parse WebSocket message: %s", message)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("处理WebSocket消息时出错: %s, 频道: %s, 原始消息: %s", error, channel_name, message)

    async def _broadcast_channel_message(
        self,
        message: Dict[str, Any],
        sender: web.WebSocketResponse,
        channel_name: str,
    ) -> None:
        """Broadcast channel messages based on channel affinity."""

        encoded = json.dumps(message, ensure_ascii=False)

        if channel_name == "global":
            for clients in self._channel_map.values():
                await self._fanout(encoded, sender, clients)
        else:
            await self._fanout(encoded, sender, self._channel_map.get(channel_name, set()))

    @staticmethod
    async def _fanout(
        payload: str,
        sender: web.WebSocketResponse,
        clients: Set[web.WebSocketResponse],
    ) -> None:
        for client in list(clients):
            if client == sender or client.closed:
                continue
            try:
                await client.send_str(payload)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("Failed to broadcast to client: %s", exc)

    async def _gracefully_close_channels(self, _app: Optional[web.Application]) -> None:
        """Close open WebUI websocket channels when the server shuts down."""

        close_tasks = []
        for channel_clients in list(self._channel_map.values()):
            for client in list(channel_clients):
                close_tasks.append(client.close(code=1001, message=b"Server shutting down"))
            channel_clients.clear()
        self._channel_map.clear()

        for root_ws in list(self._root_connections):
            close_tasks.append(root_ws.close(code=1001, message=b"Server shutting down"))
        self._root_connections.clear()

        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)


__all__ = ["WebUIServer", "AioHTTPWebSocketAdapter"]
