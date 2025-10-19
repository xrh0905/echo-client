"""WebUI HTTP and WebSocket server implementation."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)


class WebUIServer:
    """HTTP and WebSocket server for the echo-client WebUI."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3000,
        webui_root: str = "echoliveui",
        save_endpoint: str = "/api/save",
        websocket_path: str = "/ws",
    ) -> None:
        self.host = host
        self.port = port
        self.webui_root = Path(webui_root)
        self.save_endpoint = save_endpoint
        self.websocket_path = websocket_path
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.channel_map: Dict[str, Set[web.WebSocketResponse]] = {}

    async def start(self) -> None:
        """Start the WebUI server."""
        self.app = web.Application()
        self._setup_routes()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

        logger.info("WebUI server started at http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        """Stop the WebUI server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        logger.info("WebUI server stopped")

    def _setup_routes(self) -> None:
        """Configure HTTP and WebSocket routes."""
        if self.app is None:
            return

        # WebSocket route with optional channel parameter
        self.app.router.add_get(f"{self.websocket_path}{{tail:.*}}", self._handle_websocket)

        # API endpoint for saving configuration
        self.app.router.add_post(self.save_endpoint, self._handle_save_config)

        # Static file serving for WebUI
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_static("/", self.webui_root, show_index=False)

    async def _handle_index(self, _request: web.Request) -> web.Response:
        """Serve the index.html (editor.html) file."""
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
            return web.json_response(
                {"error": "保存配置失败: 无效的 JSON 数据"},
                status=400
            )

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
            root_path = Path(root)
            if not root_path.exists():
                logger.debug("目录不存在，创建目录: %s", root)
                root_path.mkdir(parents=True, exist_ok=True)

            file_path = root_path / name
            logger.debug("准备写入文件: %s", file_path)

            file_path.write_text(
                json.dumps(config_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
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
            return web.json_response(
                {"error": str(error)},
                status=500
            )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """Handle WebSocket connections with channel support."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Parse channel name from URL path
        url_path = request.match_info.get("tail", "")
        normalized_path = url_path.rstrip("/")

        # Extract channel name
        if normalized_path:
            parts = normalized_path.split("/")
            channel_name = parts[1] if len(parts) > 1 else "global"
        else:
            channel_name = "global"

        logger.info("新的WebSocket连接，客户端地址: %s, 频道: %s",
                   request.remote, channel_name)

        # Register client to channel
        if channel_name not in self.channel_map:
            self.channel_map[channel_name] = set()
        self.channel_map[channel_name].add(ws)

        # Setup ping interval
        ping_task = asyncio.create_task(self._ping_client(ws, channel_name))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle_websocket_message(msg.data, ws, channel_name)
                elif msg.type == WSMsgType.ERROR:
                    logger.error("WebSocket错误，频道: %s: %s",
                               channel_name, ws.exception())
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

            # Cleanup
            logger.info("WebSocket客户端断开连接，频道: %s", channel_name)
            if channel_name in self.channel_map:
                self.channel_map[channel_name].discard(ws)
                if not self.channel_map[channel_name]:
                    del self.channel_map[channel_name]

        return ws

    async def _ping_client(self, ws: web.WebSocketResponse, channel_name: str) -> None:
        """Send periodic ping to keep connection alive."""
        try:
            while not ws.closed:
                await asyncio.sleep(30)
                if not ws.closed:
                    await ws.ping()
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Ping error for channel %s: %s", channel_name, e)

    async def _handle_websocket_message(
        self,
        message: str,
        ws: web.WebSocketResponse,
        channel_name: str
    ) -> None:
        """Process incoming WebSocket messages and broadcast."""
        try:
            parsed_msg = json.loads(message)
            logger.debug("收到WebSocket消息，频道: %s, 消息: %s", channel_name, message)

            if not parsed_msg or "from" not in parsed_msg or "data" not in parsed_msg:
                logger.warning("收到格式不正确的消息: %s", message)
                return

            from_info = parsed_msg.get("from", {})
            action = parsed_msg.get("action")
            target = parsed_msg.get("target")

            # Log different message types
            from_type = from_info.get("type")
            from_uuid = from_info.get("uuid")

            if from_type == "live":
                if action == "hello" and target is None:
                    logger.info("对话框加入服务器, UUID: %s, 频道: %s",
                              from_uuid, channel_name)
                elif action == "close" and target is None:
                    logger.info("对话框离开服务器, UUID: %s, 频道: %s",
                              from_uuid, channel_name)
            elif from_type == "history":
                if action == "hello" and target is None:
                    logger.info("历史记录浏览器加入服务器, UUID: %s, 频道: %s",
                              from_uuid, channel_name)
                elif action == "close" and target is None:
                    logger.info("历史记录浏览器离开服务器, UUID: %s, 频道: %s",
                              from_uuid, channel_name)
            elif from_type == "server":
                if action == "ping" and target is None:
                    logger.info("编辑器加入服务器, UUID: %s, 频道: %s",
                              from_uuid, channel_name)
            else:
                logger.warning("收到未定义类型的消息: %s, 频道: %s, 原始: %s",
                             from_type, channel_name, message)

            # Broadcast logic
            await self._broadcast_message(parsed_msg, ws, channel_name)

        except json.JSONDecodeError:
            logger.error("Failed to parse WebSocket message: %s", message)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("处理WebSocket消息时出错: %s, 频道: %s, 原始消息: %s",
                       error, channel_name, message)

    async def _broadcast_message(
        self,
        message: Dict[str, Any],
        sender: web.WebSocketResponse,
        channel_name: str
    ) -> None:
        """Broadcast message to appropriate clients."""
        message_json = json.dumps(message, ensure_ascii=False)

        if channel_name == "global":
            # Broadcast to all channels
            for clients in self.channel_map.values():
                for client in clients:
                    if client != sender and not client.closed:
                        try:
                            await client.send_str(message_json)
                        except Exception as e:  # pylint: disable=broad-exception-caught
                            logger.error("Failed to broadcast to client: %s", e)
        else:
            # Broadcast only to current channel
            if channel_name in self.channel_map:
                for client in self.channel_map[channel_name]:
                    if client != sender and not client.closed:
                        try:
                            await client.send_str(message_json)
                        except Exception as e:  # pylint: disable=broad-exception-caught
                            logger.error("Failed to broadcast to client: %s", e)


__all__ = ["WebUIServer"]
