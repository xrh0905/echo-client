"""Runtime server that bridges console commands and websocket clients."""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
import signal
import unicodedata
from typing import Any, Iterable

from aiohttp import WSCloseCode, WSMsgType, web
from rich.console import Console
from rich.table import Table

from .commands import (
    CommandCatalog,
    CommandSpec,
    argument_hint,
    build_command_specs,
    command_status,
    format_aliases,
)
from .config import load_config, save_config
from .connection import ConnectionManager, connection_is_closed
from .message import (
    apply_autopause,
    format_username,
    get_delay,
    normalize_typewriting_scheme,
    parse_message,
    render,
)
from .protocol import (
    PING_PAYLOAD,
    BroadcastEnvelope,
    ClientSession,
    ClientType,
    OutgoingEvent,
    SkipMode,
    effective_client_type,
    normalize_client_type,
    normalize_skip_mode,
    normalize_target_types,
    parse_envelope,
)


class AiohttpWebSocketAdapter:
    """Adapter exposing aiohttp WebSocket connections through the internal transport API."""

    def __init__(self, websocket: web.WebSocketResponse) -> None:
        self._websocket = websocket

    @property
    def closed(self) -> bool:
        return self._websocket.closed

    async def send(self, payload: str) -> None:
        await self._websocket.send_str(payload)

    async def close(self, code: int, reason: str) -> None:
        await self._websocket.close(code=code, message=reason.encode("utf-8"))

    async def iter_text(self):
        async for message in self._websocket:
            if message.type is WSMsgType.TEXT:
                yield message.data
            elif message.type is WSMsgType.ERROR:
                raise RuntimeError(f"websocket error: {self._websocket.exception()}")
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                break


class EchoServer:
    """Orchestrates the websocket server and the console interaction."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.config = load_config(self.console)
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._input_task: asyncio.Task | None = None
        self._server_wait_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._connections = ConnectionManager()
        self._parentheses_once = False
        self._sigint_guard_active = False
        self._sigint_original: Any | None = None
        self._sigint_suppressed = False
        self._restart_requested = False
        self._command_catalog: CommandCatalog | None = None
        self._command_specs: tuple[CommandSpec, ...] = ()
        self._sync_sigint_guard()
        self._refresh_command_catalog()

    @property
    def parentheses_pending(self) -> bool:
        return self._parentheses_once

    def _refresh_command_catalog(self) -> None:
        catalog = CommandCatalog(build_command_specs(self))
        self._command_catalog = catalog
        self._command_specs = catalog.specs

    def _persist_config(self) -> None:
        save_config(self.config, self.console)

    async def run(self) -> None:
        """Start the websocket server and the console input loop."""
        self._sync_sigint_guard()
        host = self.config["host"]
        port = self.config["port"]

        self._stop_event = asyncio.Event()
        await self._start_site(host, port)

        self.console.print(
            f"[green]已经在 {host}:{port} 监听 websocket 请求，等待 echo 客户端接入...[/green]"
        )
        self.console.print("[blue]tips: 如果没有看到成功的连接请求，可以尝试刷新一下客户端[/blue]")
        self.console.print("[green]用户输入模块加载成功，您现在可以开始输入命令了，客户端连接后会自动执行！[/green]")

        self._input_task = asyncio.create_task(self._run_input_loop())

        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self._cancel_input_task()
            await self._stop_site()
            self._server_wait_task = None

    async def shutdown(self) -> None:
        """Stop the websocket server and all connected clients."""
        self.console.print("[yellow]正在关闭服务器……[/yellow]")

        await self._close_all_clients()
        await self._stop_site()
        if self._stop_event is not None:
            self._stop_event.set()
        await self._cancel_input_task()
        self._restore_sigint_guard()

    async def _restart_server(self) -> None:
        """Restart the WebSocket server after a warm reload."""
        try:
            await self._close_all_clients()
            await self._stop_site()

            await asyncio.sleep(0.5)
            host = self.config["host"]
            port = self.config["port"]
            await self._start_site(host, port)

            self.console.print(f"[green]服务器已重启，正在 {host}:{port} 监听 websocket 请求。[/green]")
            self.console.print("[blue]tips: 客户端需要重新连接。[/blue]")
        except Exception as exc:
            self.console.print(f"[red]服务器重启失败: {exc}[/red]")
            self.console.print("[yellow]请手动重启程序。[/yellow]")

    def _create_app(self) -> web.Application:
        app = web.Application()
        self._register_routes(app)
        return app

    def _register_routes(self, app: web.Application) -> None:
        app.router.add_get("/", self._websocket_handler)
        app.router.add_get("/ws", self._websocket_handler)
        app.router.add_get("/healthz", self._health_handler)

    async def _start_site(self, host: str, port: int) -> None:
        self._app = self._create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

    async def _stop_site(self) -> None:
        runner = self._runner
        if runner is not None:
            await runner.cleanup()
        self._site = None
        self._runner = None
        self._app = None

    async def _health_handler(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "connections": len(self._connections.sessions),
                "host": self.config.get("host"),
                "port": self.config.get("port"),
            }
        )

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await self._handle_client(AiohttpWebSocketAdapter(websocket))
        return websocket

    async def _handle_client(self, websocket: AiohttpWebSocketAdapter) -> None:
        session = self._connections.register(websocket)
        client_id = session.client_id
        self.console.print(f"客户端{client_id}: 已建立连接")
        self._report_client_groups()

        sender = asyncio.create_task(self._send_events(session))
        receiver = asyncio.create_task(self._receive_messages(session))
        disconnect_reason: str | None = None

        try:
            await websocket.send(PING_PAYLOAD)
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    disconnect_reason = str(exc) or "异常关闭"
            for task in pending:
                task.cancel()
        except (ConnectionResetError, RuntimeError) as exc:
            disconnect_reason = str(exc) or "异常关闭"
        finally:
            for task in (sender, receiver):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            removed = self._connections.unregister(client_id) or session
            self._report_client_groups()
            self._print_disconnect_summary(removed, disconnect_reason)

    async def _receive_messages(self, session: ClientSession) -> None:
        websocket = session.websocket
        client_id = session.client_id
        async for raw_message in websocket.iter_text():
            data = parse_envelope(raw_message)
            if data is None:
                self.console.print(f"客户端{client_id}: 收到无法解析的消息 {raw_message}")
                continue

            action = data.get("action")
            payload = data.get("data", {})
            origin = data.get("from", {})
            if not isinstance(payload, dict):
                payload = {}
            if not isinstance(origin, dict):
                origin = {}

            match action:
                case "hello":
                    client_name = self._handle_hello_event(origin, payload, client_id=client_id)
                    if client_name:
                        self._connections.set_client_name(client_id, client_name)
                case "close":
                    self.console.print(f"客户端{client_id}: 发出下线请求")
                    session.graceful_disconnect_requested = True
                    await self._initiate_client_shutdown(websocket, client_id)
                    return
                case "page_hidden":
                    self.console.print(f"客户端{client_id}: 页面被隐藏")
                case "page_visible":
                    self.console.print(f"客户端{client_id}: 页面恢复显示")
                case "echo_printing":
                    username = payload.get("username", "?")
                    content = payload.get("message", "") or "(空)"
                    if content != "undefined":
                        self.console.print(f"客户端{client_id}: 正在打印 {username}: {content}")
                case "echo_state_update":
                    state = payload.get("state", "unknown")
                    remaining = payload.get("messagesCount")
                    if state == "ready" and remaining in (0, None):
                        continue
                    remaining_str = "未知" if remaining is None else str(remaining)
                    self.console.print(f"客户端{client_id}: 状态更新 -> {state}, 剩余消息 {remaining_str}")
                case "error":
                    name = payload.get("name", "unknown")
                    extras = {key: value for key, value in payload.items() if key != "name"}
                    extra_text = f"，详情: {extras}" if extras else ""
                    self.console.print(f"[red]客户端{client_id}: 报告错误 {name}{extra_text}[/red]")
                case "websocket_heartbeat":
                    session.heartbeat_count += 1
                case "live_display_update":
                    self._handle_live_display_update(client_id, payload)
                case "error_unknown":
                    self._handle_error_unknown(client_id, payload)
                case _:
                    self.console.print(f"客户端{client_id}: 发送了未知事件，事件原文: {data}")

    async def _send_events(self, session: ClientSession) -> None:
        websocket = session.websocket
        client_id = session.client_id
        try:
            while True:
                event: OutgoingEvent = await session.queue.get()
                if connection_is_closed(websocket):
                    return

                label = event.label or self._label_from_payload(event.payload)
                if label:
                    self.console.print(f"客户端{client_id}: 执行 {label}")
                else:
                    self.console.print(f"客户端{client_id}: 执行自定义 payload")

                if event.description:
                    self.console.print(f"客户端{client_id}: {event.description}")
                elif label == "message_data":
                    self.console.print(f"客户端{client_id}: 发送文字信息")

                try:
                    await websocket.send(event.payload)
                except (ConnectionResetError, RuntimeError):
                    self.console.print(f"客户端{client_id}: 连接已优雅关闭，停止发送事件")
                    return
                finally:
                    session.queue.task_done()

                if isinstance(event.delay, (int, float)) and event.delay > 0:
                    await asyncio.sleep(event.delay / 1000.0)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _label_from_payload(payload: str) -> str | None:
        try:
            parsed_candidate = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed_candidate, dict):
            return None
        label = parsed_candidate.get("action")
        return label if isinstance(label, str) and label else None

    def _print_disconnect_summary(self, session: ClientSession, disconnect_reason: str | None) -> None:
        client_id = session.client_id
        summary = f"客户端{client_id}: 连接已断开（收到心跳 {session.heartbeat_count} 次）"
        if session.name and session.name != f"客户端{client_id}":
            summary = f"客户端{client_id}({session.name}): 连接已断开（收到心跳 {session.heartbeat_count} 次）"
        if session.client_type not in {ClientType.UNKNOWN, ClientType.LIVE}:
            summary += f"，类型: {session.client_type.value}"
        if disconnect_reason:
            summary += f"，原因: {disconnect_reason}"
        if not session.graceful_disconnect_requested:
            summary += "，[red]未收到下线请求或未正常关闭[/red]"
        self.console.print(summary)

    async def _initiate_client_shutdown(self, websocket: Any, client_id: int) -> None:
        if connection_is_closed(websocket):
            return
        try:
            await websocket.close(code=WSCloseCode.OK, reason="Client requested shutdown")
        except Exception:
            self.console.print(f"客户端{client_id}: 连接关闭过程中出现异常，可能已被客户端终止")

    async def _cancel_input_task(self) -> None:
        task = self._input_task
        if task is None or task.done():
            self._input_task = None
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._input_task = None

    async def _close_all_clients(self) -> None:
        await self._connections.close_all()

    async def _run_input_loop(self) -> None:
        while True:
            try:
                command = await self._prompt_command("请输入命令: ")
                if not self._handle_console_command(command.strip()):
                    await self.shutdown()
                    break
            except asyncio.CancelledError:
                raise
            except EOFError:
                if not self._handle_keyboard_interrupt():
                    await self.shutdown()
                    break
            except KeyboardInterrupt:
                if not self._handle_keyboard_interrupt():
                    await self.shutdown()
                    break
        if self._input_task is asyncio.current_task():
            self._input_task = None

    async def _prompt_command(self, prompt: str) -> str:
        """Read a line from stdin without relying on prompt_toolkit."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.console.input, prompt)

    def _handle_console_command(self, command: str) -> bool:
        prefix = self.config["command_prefix"]

        if not command:
            self.console.print("[red]打个字再回车啊宝！[/red]")
            return True

        literal = self._literal_message_from_command(command, prefix)
        if literal is not None:
            self._send_literal_message(literal)
            return True

        if not command.startswith(prefix):
            self._send_literal_message(command)
            return True

        parts = command.split()
        action = parts[0][len(prefix) :].lower()
        args = parts[1:]

        catalog = self._command_catalog
        spec = catalog.lookup(action) if catalog else None
        if spec is not None:
            return self._run_command(spec, args)

        self.console.print("[red]这个命令怕是不存在吧……[/red]")
        suggestions = catalog.suggest(action, prefix) if catalog else []
        if suggestions:
            self.console.print("[blue]你是想输入 {} 吗？[/blue]".format("、".join(suggestions)))
        else:
            self.console.print("[blue]tips: 如果你想要发消息，请不要用 '/' 开头！[/blue]")
        self.console.print(f"[blue]输入 {prefix}help 查看命令列表。[/blue]")
        return True

    def _run_command(self, spec: CommandSpec, args: list[str]) -> bool:
        arg_count = len(args)
        if arg_count < spec.min_args:
            expected = "至少" if spec.max_args is None else str(spec.min_args)
            self.console.print(f"[red]命令缺少参数，需要 {expected} 个参数。[/red]")
            return True
        if spec.max_args is not None and arg_count > spec.max_args:
            self.console.print(f"[red]命令参数过多，仅支持 {spec.max_args} 个参数。[/red]")
            return True
        return spec.handler(args)

    def _cmd_rename(self, args: list[str]) -> bool:
        self.config["username"] = args[0]
        self._persist_config()
        self.console.print(f"[green]已经将显示名称更改为 {args[0]}[/green]")
        return True

    def _cmd_quit(self, _args: list[str]) -> bool:
        self.console.print("拜拜~")
        return False

    def _cmd_source(self, args: list[str]) -> bool:
        self._execute_source_file(args[0])
        return True

    def _cmd_reload(self, args: list[str]) -> bool:
        mode = args[0].lower() if args else "hot"
        if mode not in {"hot", "warm"}:
            self.console.print("[red]无效的重载模式，请使用 'hot' 或 'warm'。[/red]")
            return True
        if mode == "warm":
            return self._cmd_reload_warm()

        old_config = self.config.copy()
        self.config = load_config(self.console)
        self._sync_sigint_guard()

        changed_keys = [key for key, value in self.config.items() if old_config.get(key) != value]
        if changed_keys:
            self.console.print(f"[green]配置已重新加载（热重载），更新了以下配置项: {', '.join(changed_keys)}[/green]")
        else:
            self.console.print("[green]配置已重新加载（热重载），无变更。[/green]")
        return True

    def _cmd_reload_warm(self) -> bool:
        self.console.print("[yellow]正在执行温重载，将重启 WebSocket 服务器...[/yellow]")
        old_host = self.config.get("host")
        old_port = self.config.get("port")
        self.config = load_config(self.console)
        new_host = self.config.get("host")
        new_port = self.config.get("port")

        if old_host != new_host or old_port != new_port:
            self.console.print(f"[yellow]检测到服务器地址变更: {old_host}:{old_port} -> {new_host}:{new_port}[/yellow]")

        self._sync_sigint_guard()
        self.console.print("[green]配置已重新加载（温重载）。[/green]")
        self.console.print("[blue]提示: 温重载会断开所有客户端连接，服务器将立即重启。[/blue]")

        try:
            asyncio.get_running_loop().create_task(self._restart_server())
        except RuntimeError:
            self._restart_requested = True
            self.console.print("[yellow]当前不在事件循环中，已记录重启请求。[/yellow]")
        return True

    def _cmd_set_print_speed(self, args: list[str]) -> bool:
        try:
            value = int(args[0])
        except ValueError:
            self.console.print("[red]打印速度需要输入正整数，单位毫秒。[/red]")
            return True
        if value <= 0:
            self.console.print("[red]打印速度需要输入正整数，单位毫秒。[/red]")
            return True
        self.config["print_speed"] = value
        self._persist_config()
        self.console.print(f"[green]打印速度已设置为 {value}ms[/green]")
        return True

    def _cmd_toggle_typewriting(self, _args: list[str]) -> bool:
        self.config["typewriting"] = not self.config.get("typewriting", False)
        self._persist_config()
        self.console.print(f"[green]Typewriting 状态已经变更为 {self.config['typewriting']}[/green]")
        return True

    def _cmd_toggle_typewriting_scheme(self, _args: list[str]) -> bool:
        current = normalize_typewriting_scheme(self.config.get("typewriting_scheme"))
        next_scheme = "zhuyin" if current == "pinyin" else "pinyin"
        self.config["typewriting_scheme"] = next_scheme
        self._persist_config()
        self.console.print(f"[green]Typewriting 模式已切换为 {next_scheme}[/green]")
        return True

    def _cmd_toggle_autopause(self, _args: list[str]) -> bool:
        self.config["autopause"] = not self.config.get("autopause", False)
        self._persist_config()
        self.console.print(f"[green]autopause 状态已经变更为 {self.config['autopause']}[/green]")
        return True

    def _cmd_suffix(self, args: list[str]) -> bool:
        if not args:
            new_state = not self.config.get("auto_suffix", True)
            self.config["auto_suffix"] = new_state
            self._persist_config()
            state_label = "开启" if new_state else "关闭"
            self.console.print(f"[green]自动结尾字符功能已{state_label}[/green]")
            return True

        option = " ".join(args).strip()
        normalized = option.lower()
        if normalized in {"on", "off"}:
            new_state = normalized == "on"
            self.config["auto_suffix"] = new_state
            self._persist_config()
            state_label = "开启" if new_state else "关闭"
            self.console.print(f"[green]自动结尾字符功能已{state_label}[/green]")
            return True

        if not option:
            self.console.print("[red]结尾字符不能为空。[/red]")
            return True

        self.config["auto_suffix_value"] = option
        self._persist_config()
        self.console.print(f"[green]自动结尾字符已设置为 {option}[/green]")
        return True

    def _cmd_quote(self, args: list[str]) -> bool:
        if not args:
            self.console.print(f"[blue]当前自动引号样式: {self._quote_style_label()}[/blue]")
            self.console.print("[blue]用法: /quote <en|cn|jp|custom|none> [left] [right][/blue]")
            self.console.print("[blue]none 模式会禁用自动引号（等效于旧 /quotes off）[/blue]")
            self.console.print("[blue]自定义模式示例: /quote custom 『 』[/blue]")
            return True

        style = args[0].lower()
        if style == "none":
            self.config["quote_style"] = "none"
            self.config["quote_custom_left"] = ""
            self.config["quote_custom_right"] = ""
            self._persist_config()
            self.console.print("[green]自动引号功能已禁用（none）[/green]")
            return True

        if style in {"en", "cn", "jp"}:
            self.config["quote_style"] = style
            self._persist_config()
            self.console.print(f"[green]自动引号样式已设置为 {self._quote_style_label()}[/green]")
            return True

        if style in {"custom", "special"}:
            left = args[1] if len(args) > 1 else None
            right = args[2] if len(args) > 2 else None
            stored_left = str(self.config.get("quote_custom_left", "") or "")
            stored_right = str(self.config.get("quote_custom_right", "") or "")
            if left is None and right is None and stored_left and stored_right:
                left = stored_left
                right = stored_right
            if left is None or right is None:
                self.console.print("[red]custom 模式需要同时传入左右引号，例如: /quote custom 『 』[/red]")
                return True
            if not left.strip() or not right.strip():
                self.console.print("[red]自定义引号左右部分不能为空。[/red]")
                return True
            self.config["quote_style"] = "custom"
            self.config["quote_custom_left"] = left
            self.config["quote_custom_right"] = right
            self._persist_config()
            self.console.print(f"[green]自动引号样式已设置为 custom（{left}{right}）[/green]")
            return True

        self.console.print("[red]无效引号样式，可用 en/cn/jp/custom/none。[/red]")
        return True

    def _cmd_parentheses(self, args: list[str]) -> bool:
        if not args:
            self.config["auto_parentheses"] = not self.config.get("auto_parentheses", False)
            self._persist_config()
            self.console.print(f"[green]圆括号包装状态已经变更为 {self.config['auto_parentheses']}[/green]")
            return True

        option = args[0].lower()
        if option in {"once", "one", "next"}:
            self._parentheses_once = True
            self.console.print("[green]下一条消息将附加圆括号。[/green]")
            return True
        if option in {"on", "off"}:
            self.config["auto_parentheses"] = option == "on"
            self._persist_config()
            self.console.print(f"[green]圆括号包装状态已经设置为 {self.config['auto_parentheses']}[/green]")
            return True
        self.console.print("[red]参数无效，可使用 on/off 或 once。[/red]")
        return True

    def _cmd_toggle_username_brackets(self, _args: list[str]) -> bool:
        self.config["username_brackets"] = not self.config.get("username_brackets", False)
        self._persist_config()
        self.console.print(f"[green]用户名【】包裹状态: {self.config['username_brackets']}[/green]")
        return True

    def _cmd_toggle_interrupt_guard(self, args: list[str]) -> bool:
        if args:
            option = args[0].strip().lower()
            if option not in {"on", "off"}:
                self.console.print("[red]无效参数，可使用 on 或 off。[/red]")
                return True
            new_state = option == "on"
        else:
            new_state = not self.config.get("inhibit_ctrl_c", True)

        self.config["inhibit_ctrl_c"] = new_state
        self._persist_config()
        self._sync_sigint_guard()
        state_label = "开启" if new_state else "关闭"
        self.console.print(f"[green]Ctrl+C 退出保护当前状态: {state_label}[/green]")
        return True

    def _cmd_skip(self, args: list[str]) -> bool:
        if args:
            self.console.print("[yellow]/skip 不需要参数，已忽略额外输入。[/yellow]")

        skip_mode = normalize_skip_mode(self.config.get("skip_mode", "blank_text"))
        if skip_mode is SkipMode.ECHO_NEXT:
            self._enqueue_payload(
                BroadcastEnvelope("echo_next").to_json(),
                label="echo_next",
                description="触发 echo_next",
            )
            self.console.print("[green]已发送 echo_next 指令（由 /skip 触发）[/green]")
            return True

        if skip_mode is SkipMode.HIDE_DISPLAY:
            self._enqueue_payload(
                BroadcastEnvelope("set_live_display", {"display": False}).to_json(),
                label="set_live_display",
                description="隐藏实时展示",
                target_types={ClientType.LIVE},
            )
            self.console.print("[green]已发送隐藏实时展示指令（由 /skip 触发）[/green]")
        else:
            username_value = format_username(self.config)
            payload = BroadcastEnvelope(
                "message_data",
                {
                    "username": username_value,
                    "messages": [{"message": [{"text": ""}]}],
                },
            ).to_json()
            self._enqueue_payload(
                payload,
                label="message_data",
                description="发送空白文本",
                target_types={ClientType.LIVE},
            )
            self.console.print("[green]已向实时展示发送空白文本（由 /skip 触发）[/green]")

        self._enqueue_echo_next_for_history()
        return True

    def _enqueue_echo_next_for_history(self) -> None:
        self._enqueue_payload(
            BroadcastEnvelope("echo_next").to_json(),
            label="echo_next",
            description="触发 echo_next（历史客户端）",
            target_types={ClientType.HISTORY},
        )
        self.console.print("[green]已向历史客户端发送 echo_next 指令（由 /skip 触发）[/green]")

    def _cmd_clear(self, args: list[str]) -> bool:
        if args:
            self.console.print("[yellow]/clear 不需要参数，已忽略额外输入。[/yellow]")
        self._enqueue_payload(BroadcastEnvelope("history_clear").to_json(), label="history_clear", description="清空历史记录")
        self.console.print("[green]已发送清空历史记录指令（由 /clear 触发）[/green]")
        return True

    def _cmd_help(self, args: list[str]) -> bool:
        prefix = self.config["command_prefix"]
        catalog = self._command_catalog
        if catalog is None:
            self.console.print("[red]命令系统尚未初始化。[/red]")
            return True

        if args:
            query = args[0].lower()
            spec = catalog.lookup(query)
            if spec is None:
                self.console.print("[red]没有找到这个命令。[/red]")
                suggestions = catalog.suggest(query, prefix)
                if suggestions:
                    self.console.print("[blue]你是想输入 {} 吗？[/blue]".format("、".join(suggestions)))
                else:
                    self.console.print(f"[blue]输入 {prefix}help 查看全部命令。[/blue]")
                return True
            self._print_command_details(spec, prefix)
            return True

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("命令")
        table.add_column("常用别名")
        table.add_column("当前值")
        table.add_column("参数")
        table.add_column("说明", overflow="fold")
        for spec in catalog.specs:
            table.add_row(
                f"{prefix}{spec.name}",
                format_aliases(spec.aliases, prefix),
                command_status(self, spec),
                argument_hint(spec),
                spec.description or "-",
            )
        self.console.print(table)
        return True

    def _print_command_details(self, spec: CommandSpec, prefix: str) -> None:
        usage = prefix + spec.name
        if spec.max_args == 1:
            placeholder = "[value]" if spec.min_args == 0 else "<value>"
            usage = f"{usage} {placeholder}"
        elif spec.max_args not in (0, None):
            usage = f"{usage} <args>"
        elif spec.max_args is None:
            usage = f"{usage} <...>"

        self.console.print(f"[cyan]{usage}[/cyan] - {spec.description or '无描述'}")
        alias_text = format_aliases(spec.aliases, prefix)
        if alias_text != "-":
            self.console.print(f"[white]常用别名[/white]: {alias_text}")
        self.console.print(f"[white]参数[/white]: {argument_hint(spec)}")
        status = command_status(self, spec)
        if status != "-":
            self.console.print(f"[white]当前值[/white]: {status}")

    @staticmethod
    def _literal_message_from_command(command: str, prefix: str) -> str | None:
        if not prefix or not command.startswith(prefix * 2):
            return None
        repeats = 0
        step = len(prefix)
        index = 0
        while command.startswith(prefix, index):
            repeats += 1
            index += step
        remainder = command[index:]
        return prefix * (repeats - 1) + remainder

    def _execute_source_file(self, path: str) -> None:
        file_path = Path(path).expanduser()
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        self.console.print(f"[blue]从文件 {file_path} 中载入内容（文件中的每一行会被作为独立的部分输入到控制台里！）[/]")
        try:
            with file_path.open("r", encoding="utf-8") as file:
                for line in file:
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    self.console.print(f"[blue]（自动执行）[/blue]请输入命令：{text}")
                    if not self._handle_console_command(text):
                        break
        except FileNotFoundError:
            self.console.print("[red]这个文件怕是不存在吧！已终止后续的解析！[/]")

    @staticmethod
    def _is_wrapped(text: str, left: str, right: str) -> bool:
        return len(text) >= len(left) + len(right) and text.startswith(left) and text.endswith(right)

    def _decorate_outgoing_text(self, text: str) -> str:
        result = text
        left_quote, right_quote = self._quote_pair()
        if left_quote and right_quote and not self._is_wrapped(result, left_quote, right_quote):
            result = f"{left_quote}{result}{right_quote}"
        apply_parentheses = self.config.get("auto_parentheses", False) or self._parentheses_once
        if apply_parentheses and not self._is_wrapped(result, "(", ")"):
            result = f"({result})"
        self._parentheses_once = False
        return result

    def _quote_style_label(self) -> str:
        style = str(self.config.get("quote_style", "en") or "en").lower()
        if style == "none":
            return "none（禁用自动引号）"
        if style == "cn":
            return "cn “ ”"
        if style == "jp":
            return "jp 『 』"
        if style == "custom":
            left = str(self.config.get("quote_custom_left", "") or "")
            right = str(self.config.get("quote_custom_right", "") or "")
            if left and right:
                return f"custom（{left}{right}）"
            return "custom（未配置）"
        return 'en ""'

    def _quote_pair(self) -> tuple[str, str]:
        style = str(self.config.get("quote_style", "en") or "en").lower()
        if style == "none":
            return "", ""
        if style == "cn":
            return "“", "”"
        if style == "jp":
            return "『", "』"
        if style == "custom":
            left = str(self.config.get("quote_custom_left", "") or "")
            right = str(self.config.get("quote_custom_right", "") or "")
            if left and right:
                return left, right
        return '"', '"'

    def _apply_auto_suffix(self, text: str) -> str:
        if not isinstance(text, str) or text == "":
            return text
        if not self.config.get("auto_suffix", True):
            return text
        suffix = str(self.config.get("auto_suffix_value", "喵"))
        if not suffix:
            return text
        trimmed = text.rstrip()
        if not trimmed or trimmed.endswith(suffix):
            return text
        if not any(self._is_semantic_character(char) for char in trimmed):
            return text
        trailing = text[len(trimmed) :]
        return f"{trimmed}{suffix}{trailing}"

    @staticmethod
    def _is_semantic_character(char: str) -> bool:
        if char.isalnum():
            return True
        category = unicodedata.category(char)
        return bool(category) and category[0] in {"L", "N", "S"}

    def _enqueue_payload(
        self,
        payload: str,
        *,
        delay: int | float | None = None,
        label: str | None = None,
        description: str | None = None,
        target_types: Iterable[str | ClientType] | None = None,
    ) -> None:
        event = OutgoingEvent(
            payload=payload,
            delay=delay if isinstance(delay, (int, float)) and delay > 0 else None,
            label=label,
            description=description,
            target_types=normalize_target_types(target_types),
        )
        self._connections.enqueue(event)

    def _enqueue_message(self, text: str) -> None:
        syntax = parse_message(text)
        syntax = apply_autopause(self.config, syntax)
        payload = render(self.config, syntax)
        delay = get_delay(self.config, syntax)
        self._enqueue_payload(payload, delay=delay, label="message_data", description="发送文字信息")

    def _send_literal_message(self, text: str) -> None:
        enriched = self._apply_auto_suffix(text)
        decorated = self._decorate_outgoing_text(enriched)
        self.console.print(f"发送文字消息: {decorated}")
        self._enqueue_message(decorated)

    def _connection_is_closed(self, websocket: Any) -> bool:
        return connection_is_closed(websocket)

    def _handle_live_display_update(self, client_id: int, payload: dict[str, Any]) -> None:
        display_state = bool(payload.get("display"))
        session = self._connections.get(client_id)
        previous = session.live_display_visible if session is not None else None
        if session is not None:
            session.live_display_visible = display_state

        state_label = "开启" if display_state else "关闭"
        extra = "，状态未变化" if previous is not None and previous == display_state else ""
        vanish_hint = "（自动消隐）" if not display_state else ""
        self.console.print(f"客户端{client_id}: 实时展示 {state_label}{vanish_hint}{extra}")

    def _handle_error_unknown(self, client_id: int, payload: dict[str, Any]) -> None:
        message = payload.get("message", "未知错误")
        source = payload.get("source", "")
        line = payload.get("line", 0)
        col = payload.get("col", 0)

        error_parts = [f"[red]客户端{client_id}: 客户端报告错误[/red]"]
        error_parts.append(f"  [yellow]消息:[/yellow] {message}")
        if source and source not in {"null", "undefined"}:
            error_parts.append(f"  [yellow]来源:[/yellow] {source}")
        if line > 0 or col > 0:
            location = []
            if line > 0:
                location.append(f"行 {line}")
            if col > 0:
                location.append(f"列 {col}")
            error_parts.append(f"  [yellow]位置:[/yellow] {', '.join(location)}")
        self.console.print("\n".join(error_parts))

    def _handle_hello_event(
        self,
        origin: dict[str, Any],
        payload: dict[str, Any],
        *,
        client_id: int | None = None,
    ) -> str | None:
        client_name = origin.get("name") or origin.get("uuid")
        client_type = origin.get("type")
        hidden = payload.get("hidden")
        targeted = payload.get("targeted")

        status_bits: list[str] = []
        if isinstance(client_type, str) and client_type:
            status_bits.append(f"类型: {client_type}")
        if hidden is True:
            status_bits.append("隐藏")
        elif hidden is False:
            status_bits.append("可见")
        if targeted:
            status_bits.append("定向模式")

        status_text = f"，状态: {', '.join(status_bits)}" if status_bits else ""
        if client_id is not None:
            label = f"客户端{client_id}"
            if client_name and client_name != label:
                label = f"{label}({client_name})"
            if isinstance(client_type, str) and client_type:
                self._connections.set_client_type(client_id, client_type)
                self._report_client_groups()
            self.console.print(f"{label}: 上线{status_text}")
        return str(client_name) if client_name else None

    def _normalize_client_type(self, client_type: Any) -> str:
        return normalize_client_type(client_type).value

    def _effective_client_type(self, client_id: int) -> str:
        session = self._connections.get(client_id)
        if session is None:
            return ClientType.LIVE.value
        return effective_client_type(session.client_type).value

    def _report_client_groups(self) -> None:
        summary = self._connections.group_summary()
        if summary:
            self.console.print("[dim]客户端分组 -> " + summary + "[/dim]")

    def _sync_sigint_guard(self) -> None:
        if self.config.get("inhibit_ctrl_c", True):
            self._install_sigint_guard()
        else:
            self._restore_sigint_guard()

    def _install_sigint_guard(self) -> None:
        if self._sigint_guard_active:
            return
        try:
            self._sigint_original = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._sigint_handler)
            self._sigint_guard_active = True
            self._sigint_suppressed = False
        except (ValueError, OSError):
            self._sigint_original = None
            self._sigint_guard_active = False

    def _restore_sigint_guard(self) -> None:
        if not self._sigint_guard_active:
            return
        handler = self._sigint_original if self._sigint_original is not None else signal.SIG_DFL
        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            pass
        self._sigint_guard_active = False
        self._sigint_original = None
        self._sigint_suppressed = False

    def _sigint_handler(self, signum: int, frame: Any) -> None:  # pragma: no cover - signal path
        if self.config.get("inhibit_ctrl_c", True):
            self._sigint_suppressed = True
            self._warn_ctrl_c_guard()
            return

        previous = self._sigint_original
        self._restore_sigint_guard()
        if callable(previous):
            previous(signum, frame)
            return
        if previous == signal.SIG_IGN:
            return
        raise KeyboardInterrupt

    def _warn_ctrl_c_guard(self) -> None:
        self.console.print("[yellow]检测到 Ctrl+C，但当前启用了退出保护；请使用 /nocc 关闭保护或使用 /quit 正常退出。[/yellow]")

    def _handle_keyboard_interrupt(self) -> bool:
        if not self.config.get("inhibit_ctrl_c", True):
            return False
        if self._sigint_suppressed:
            self._sigint_suppressed = False
            return True
        self._warn_ctrl_c_guard()
        return True


__all__ = ["EchoServer"]
