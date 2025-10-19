"""Runtime server that bridges console commands and websocket clients."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import signal
import unicodedata
from typing import Any, Awaitable, Optional

import websockets
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
from .message import (
    apply_autopause,
    get_delay,
    normalize_typewriting_scheme,
    parse_message,
    render,
)

PING_PAYLOAD = json.dumps({"action": "ping", "data": {}}, ensure_ascii=False)


class EchoServer:
    """Orchestrates the websocket server and console interaction."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.config = load_config(self.console)
        self._events: list[dict[str, Any]] = []
        self._client_ids = itertools.count(1)
        self._server: Any | None = None
        self._heartbeat_counts: dict[int, int] = {}
        self._client_names: dict[int, str] = {}
        self._client_types: dict[int, str] = {}
        self._live_display_visibility: dict[int, bool] = {}
        self._graceful_disconnect_requests: dict[int, bool] = {}
        self._parentheses_once: bool = False
        self._sigint_guard_active = False
        self._sigint_original: Any | None = None
        self._sigint_suppressed = False
        self._command_catalog: CommandCatalog | None = None
        self._command_specs: tuple[CommandSpec, ...] = ()
        self._webui_server: Any | None = None
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

        server_waiter: Awaitable[Any] | None = None

        # Start WebUI server if enabled
        if self.config.get("enable_webui", False):
            await self._start_webui_server()
            if self._webui_server is not None:
                server_waiter = self._webui_server.wait_closed()
            else:
                self.console.print(
                    "[yellow]WebUI 模块启动失败，正在回退到基础 websocket 服务[/yellow]"
                )

        if self._webui_server is None:
            self._server = await websockets.serve(self._handle_client, host, port)
            server_waiter = self._server.wait_closed()

        self.console.print(
            f"[green]已经在 {host}:{port} 监听 websocket 请求，等待 echo 客户端接入...[/green]"
        )
        self.console.print("[blue]tips: 如果没有看到成功的连接请求，可以尝试刷新一下客户端[/blue]")
        self.console.print("[green]用户输入模块加载成功，您现在可以开始输入命令了，客户端连接后会自动执行！[/green]")

        asyncio.create_task(self._run_input_loop())

        if server_waiter is not None:
            await server_waiter

    async def shutdown(self) -> None:
        """Stop the websocket server."""
        self.console.print("[yellow]正在关闭服务器……[/yellow]")

        # Stop WebUI server if running
        if self._webui_server is not None:
            await self._stop_webui_server()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._restore_sigint_guard()

    async def _start_webui_server(self) -> None:
        """Start the WebUI HTTP/WebSocket server."""
        try:
            from .echo_webui import WebUIServer

            host = self.config.get("host", "127.0.0.1")
            port = self.config.get("port", 3000)
            webui_root = self.config.get("webui_root", "echoliveui")
            save_endpoint = self.config.get("webui_save_endpoint", "/api/save")
            websocket_path = self.config.get("webui_websocket_path", "/ws")

            modules = self.config.get("webui_modules", ["echo_webui"])

            self._webui_server = WebUIServer(
                host=host,
                port=port,
                webui_root=webui_root,
                save_endpoint=save_endpoint,
                websocket_path=websocket_path,
                modules=modules,
                config_context={
                    "websocketPath": websocket_path,
                    "modulesEnabled": modules,
                },
                control_callback=self._handle_webui_control,
            )
            await self._webui_server.start(self._handle_client)
            self.console.print(
                f"[green]WebUI 服务器已启动在 http://{host}:{port}[/green]"
            )
        except Exception as e:
            self.console.print(f"[red]启动 WebUI 服务器失败: {e}[/red]")
            self._webui_server = None

    async def _stop_webui_server(self) -> None:
        """Stop the WebUI HTTP/WebSocket server."""
        if self._webui_server is not None:
            try:
                await self._webui_server.stop()
                self.console.print("[yellow]WebUI 服务器已停止[/yellow]")
            except Exception as e:
                self.console.print(f"[red]停止 WebUI 服务器时出错: {e}[/red]")
            finally:
                self._webui_server = None

    async def _handle_client(self, websocket: Any) -> None:
        client_id = next(self._client_ids)
        self.console.print(f"客户端{client_id}: 已建立连接")
        self._heartbeat_counts[client_id] = 0
        self._client_names[client_id] = f"客户端{client_id}"
        self._client_types[client_id] = "unknown"
        self._live_display_visibility[client_id] = False
        self._graceful_disconnect_requests[client_id] = False

        listener = asyncio.create_task(self._pump_events(websocket, client_id))
        receiver = asyncio.create_task(self._receive_messages(websocket, client_id))

        disconnect_reason: Optional[str] = None

        try:
            await websocket.send(PING_PAYLOAD)
            await asyncio.gather(listener, receiver)
        except websockets.exceptions.ConnectionClosed as exc:
            disconnect_reason = f"代码 {exc.code}" if hasattr(exc, "code") else "异常关闭"
        finally:
            for task in (listener, receiver):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            heartbeat_count = self._heartbeat_counts.pop(client_id, 0)
            client_name = self._client_names.pop(client_id, None)
            client_type = self._client_types.pop(client_id, None)
            self._live_display_visibility.pop(client_id, None)
            graceful = self._graceful_disconnect_requests.pop(client_id, False)
            summary = f"客户端{client_id}: 连接已断开（收到心跳 {heartbeat_count} 次）"
            if client_name and client_name != f"客户端{client_id}":
                summary = (
                    f"客户端{client_id}({client_name}): 连接已断开（收到心跳 {heartbeat_count} 次）"
                )
            if client_type and client_type != "unknown":
                summary += f"，类型: {client_type}"
            if disconnect_reason:
                summary += f"，原因: {disconnect_reason}"
            if not graceful:
                summary += "，[red]未收到下线请求或未正常关闭[/red]"
            self.console.print(summary)

    async def _receive_messages(self, websocket: Any, client_id: int) -> None:
        async for raw_message in websocket:
            try:
                data = json.loads(raw_message)
            except json.JSONDecodeError:
                self.console.print(f"客户端{client_id}: 收到无法解析的消息 {raw_message}")
                continue

            action = data.get("action")
            payload = data.get("data", {})
            origin = data.get("from", {})

            match action:
                case "hello":
                    client_name = self._handle_hello_event(origin, payload, client_id=client_id)
                    if client_name:
                        self._client_names[client_id] = client_name
                case "close":
                    self.console.print(f"客户端{client_id}: 发出下线请求")
                    self._graceful_disconnect_requests[client_id] = True
                    await self._initiate_client_shutdown(websocket, client_id)
                    return
                case "page_hidden":
                    self.console.print(f"客户端{client_id}: 页面被隐藏")
                case "page_visible":
                    self.console.print(f"客户端{client_id}: 页面恢复显示")
                case "echo_printing":
                    username = payload.get("username", "?")
                    content = payload.get("message", "") or "(空)"
                    if content == "undefined":
                        continue
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
                    extras = {k: v for k, v in payload.items() if k != "name"}
                    extra_text = f"，详情: {extras}" if extras else ""
                    self.console.print(f"[red]客户端{client_id}: 报告错误 {name}{extra_text}[/red]")
                case "websocket_heartbeat":
                    self._heartbeat_counts[client_id] = self._heartbeat_counts.get(client_id, 0) + 1
                case "live_display_update":
                    self._handle_live_display_update(client_id, payload)
                case _:
                    if origin.get("type") == "server" and self._relay_server_action(
                        data,
                        raw_message,
                        client_id=client_id,
                    ):
                        continue

                    self.console.print(f"客户端{client_id}: 发送了未知事件，事件原文: {data}")

    async def _pump_events(self, websocket: Any, client_id: int) -> None:
        proceed = len(self._events)
        try:
            while True:
                await asyncio.sleep(0.1)
                if self._connection_is_closed(websocket):
                    return
                if proceed >= len(self._events):
                    continue

                for event in self._events[proceed:]:
                    payload = event.get("payload")
                    if not isinstance(payload, str):
                        self.console.print(
                            f"[red]客户端{client_id}: 事件缺少可发送的 payload，已忽略[/red]"
                        )
                        continue

                    label = event.get("label")
                    if label is None:
                        try:
                            parsed_candidate = json.loads(payload)
                        except json.JSONDecodeError:
                            parsed_candidate = None
                        if isinstance(parsed_candidate, dict):
                            raw_label = parsed_candidate.get("action")
                            if isinstance(raw_label, str) and raw_label:
                                label = raw_label

                    if not isinstance(label, str) or not label:
                        label = None

                    description = event.get("description")
                    if label:
                        self.console.print(f"客户端{client_id}: 执行 {label}")
                    else:
                        self.console.print(f"客户端{client_id}: 执行自定义 payload")

                    if description:
                        self.console.print(f"客户端{client_id}: {description}")
                    elif label == "message_data":
                        self.console.print(f"客户端{client_id}: 发送文字信息")

                    try:
                        await websocket.send(payload)
                    except websockets.exceptions.ConnectionClosedOK:
                        self.console.print(
                            f"客户端{client_id}: 连接已优雅关闭，停止发送事件"
                        )
                        return
                    except websockets.exceptions.ConnectionClosed as exc:
                        code_repr = getattr(exc, "code", "?")
                        self.console.print(
                            f"客户端{client_id}: 无法发送事件，连接已关闭 ({code_repr})"
                        )
                        return

                    delay_value = event.get("delay")
                    if isinstance(delay_value, (int, float)) and delay_value > 0:
                        await asyncio.sleep(delay_value / 1000.0)
                proceed = len(self._events)
        except asyncio.CancelledError:
            pass

    async def _initiate_client_shutdown(self, websocket: Any, client_id: int) -> None:
        if self._connection_is_closed(websocket):
            return
        try:
            await websocket.close(code=1000, reason="Client requested shutdown")
        except websockets.exceptions.ConnectionClosed:
            self.console.print(
                f"客户端{client_id}: 连接关闭过程中出现异常，可能已被客户端终止"
            )

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
            self.console.print(
                "[blue]你是想输入 {} 吗？[/blue]".format(
                    "、".join(suggestions)
                )
            )
        else:
            self.console.print("[blue]tips: 如果你想要发消息，请不要用 '/' 开头！[/blue]")
        self.console.print(f"[blue]输入 {prefix}help 查看命令列表。[/blue]")
        return True

    def _run_command(self, spec: CommandSpec, args: list[str]) -> bool:
        arg_count = len(args)
        if arg_count < spec.min_args:
            expected = "至少" if spec.max_args is None else str(spec.min_args)
            self.console.print(
                f"[red]命令缺少参数，需要 {expected} 个参数。[/red]"
            )
            return True

        if spec.max_args is not None and arg_count > spec.max_args:
            self.console.print(
                f"[red]命令参数过多，仅支持 {spec.max_args} 个参数。[/red]"
            )
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
        self.console.print(
            f"[green]Typewriting 状态已经变更为 {self.config['typewriting']}[/green]"
        )
        return True

    def _cmd_toggle_typewriting_scheme(self, _args: list[str]) -> bool:
        current = normalize_typewriting_scheme(self.config.get("typewriting_scheme"))
        next_scheme = "zhuyin" if current == "pinyin" else "pinyin"
        self.config["typewriting_scheme"] = next_scheme
        self._persist_config()
        self.console.print(
            f"[green]Typewriting 模式已切换为 {next_scheme}[/green]"
        )
        return True

    def _cmd_toggle_autopause(self, _args: list[str]) -> bool:
        self.config["autopause"] = not self.config.get("autopause", False)
        self._persist_config()
        self.console.print(
            f"[green]autopause 状态已经变更为 {self.config['autopause']}[/green]"
        )
        return True

    def _cmd_toggle_quotes(self, _args: list[str]) -> bool:
        self.config["auto_quotes"] = not self.config.get("auto_quotes", True)
        self._persist_config()
        self.console.print(
            f"[green]自动双引号包装当前状态: {self.config['auto_quotes']}[/green]"
        )
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

        suffix_value = option
        if not suffix_value:
            self.console.print("[red]结尾字符不能为空。[/red]")
            return True

        self.config["auto_suffix_value"] = suffix_value
        self._persist_config()
        self.console.print(f"[green]自动结尾字符已设置为 {suffix_value}[/green]")
        return True

    def _cmd_parentheses(self, args: list[str]) -> bool:
        if not args:
            self.config["auto_parentheses"] = not self.config.get("auto_parentheses", False)
            self._persist_config()
            self.console.print(
                f"[green]圆括号包装状态已经变更为 {self.config['auto_parentheses']}[/green]"
            )
            return True

        option = args[0].lower()
        if option in {"once", "one", "next"}:
            self._parentheses_once = True
            self.console.print("[green]下一条消息将附加圆括号。[/green]")
            return True

        if option in {"on", "off"}:
            self.config["auto_parentheses"] = option == "on"
            self._persist_config()
            self.console.print(
                f"[green]圆括号包装状态已经设置为 {self.config['auto_parentheses']}[/green]"
            )
            return True

        self.console.print("[red]参数无效，可使用 on/off 或 once。[/red]")
        return True

    def _cmd_toggle_username_brackets(self, _args: list[str]) -> bool:
        self.config["username_brackets"] = not self.config.get("username_brackets", False)
        self._persist_config()
        self.console.print(
            f"[green]用户名【】包裹状态: {self.config['username_brackets']}[/green]"
        )
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
        self.console.print(
            f"[green]Ctrl+C 退出保护当前状态: {state_label}[/green]"
        )
        return True

    def _cmd_skip(self, args: list[str]) -> bool:
        if args:
            self.console.print("[yellow]/skip 不需要参数，已忽略额外输入。[/yellow]")
        payload = json.dumps({"action": "echo_next", "data": {}}, ensure_ascii=False)
        self._enqueue_payload(payload, label="echo_next", description="触发 echo_next")
        self.console.print("[green]已加入 echo_next 指令（由 /skip 触发）[/green]")
        return True

    def _cmd_toggle_webui(self, args: list[str]) -> bool:
        if args:
            option = args[0].strip().lower()
            if option not in {"on", "off"}:
                self.console.print("[red]无效参数，可使用 on 或 off。[/red]")
                return True
            new_state = option == "on"
        else:
            new_state = not self.config.get("enable_webui", False)

        self.config["enable_webui"] = new_state
        self._persist_config()
        state_label = "开启" if new_state else "关闭"
        
        if new_state:
            self.console.print(
                f"[green]WebUI 已{state_label}，请重启服务器以应用更改[/green]"
            )
        else:
            self.console.print(
                f"[yellow]WebUI 已{state_label}，请重启服务器以应用更改[/yellow]"
            )
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
                    self.console.print(
                        "[blue]你是想输入 {} 吗？[/blue]".format("、".join(suggestions))
                    )
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
        if any(spec.legacy_aliases for spec in catalog.specs):
            self.console.print("[dim]提示: 历史命令仍可用，但推荐优先使用表格中的短别名。[/dim]")
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

        if spec.legacy_aliases:
            legacy_aliases = ", ".join(f"{prefix}{alias}" for alias in spec.legacy_aliases)
            self.console.print(f"[white]历史别名[/white]: {legacy_aliases}")

        self.console.print(f"[white]参数[/white]: {argument_hint(spec)}")
        status = command_status(self, spec)
        if status != "-":
            self.console.print(f"[white]当前值[/white]: {status}")

    @staticmethod
    def _literal_message_from_command(command: str, prefix: str) -> Optional[str]:
        if not prefix:
            return None
        if not command.startswith(prefix * 2):
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
        from pathlib import Path

        file_path = Path(path).expanduser()
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path

        self.console.print(
            f"[blue]从文件 {file_path} 中载入内容（文件中的每一行会被作为独立的部分输入到控制台里！）[/]"
        )
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
        if self.config.get("auto_quotes", True) and not self._is_wrapped(result, '"', '"'):
            result = f'"{result}"'

        apply_parentheses = self.config.get("auto_parentheses", False) or self._parentheses_once
        if apply_parentheses and not self._is_wrapped(result, "(", ")"):
            result = f"({result})"

        self._parentheses_once = False
        return result

    def _apply_auto_suffix(self, text: str) -> str:
        if not isinstance(text, str) or text == "":
            return text
        if not self.config.get("auto_suffix", True):
            return text

        suffix = str(self.config.get("auto_suffix_value", "喵"))
        if not suffix:
            return text

        trimmed = text.rstrip()
        if not trimmed:
            return text

        if trimmed.endswith(suffix):
            return text

        if not any(self._is_semantic_character(ch) for ch in trimmed):
            return text

        trailing = text[len(trimmed):]
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
    ) -> None:
        event: dict[str, Any] = {"payload": payload}
        if label:
            event["label"] = label
        if description:
            event["description"] = description
        if isinstance(delay, (int, float)) and delay > 0:
            event["delay"] = delay
        self._events.append(event)

    def _enqueue_message(self, text: str) -> None:
        syntax = parse_message(text)
        syntax = apply_autopause(self.config, syntax)
        payload = render(self.config, syntax)
        delay = get_delay(self.config, syntax)
        self._enqueue_payload(
            payload,
            delay=delay,
            label="message_data",
            description="发送文字信息",
        )

    def _send_literal_message(self, text: str) -> None:
        enriched = self._apply_auto_suffix(text)
        decorated = self._decorate_outgoing_text(enriched)
        self.console.print(f"发送文字消息: {decorated}")
        self._enqueue_message(decorated)

    def _connection_is_closed(self, websocket: Any) -> bool:
        closed_attr = getattr(websocket, "closed", None)
        if isinstance(closed_attr, bool):
            return closed_attr
        if callable(closed_attr):
            try:
                result = closed_attr()
            except TypeError:
                result = None
            if isinstance(result, bool):
                return result

        state = getattr(websocket, "state", None)
        if state is not None:
            state_name = getattr(state, "name", "")
            if state_name in {"CLOSING", "CLOSED"}:
                return True
            if state_name == "OPEN":
                return False

        return False

    def _handle_live_display_update(self, client_id: int, payload: dict[str, Any]) -> None:
        display_state = bool(payload.get("display"))
        previous = self._live_display_visibility.get(client_id)
        self._live_display_visibility[client_id] = display_state

        state_label = "开启" if display_state else "关闭"
        extra = "，状态未变化" if previous is not None and previous == display_state else ""
        vanish_hint = "（自动消隐）" if not display_state else ""
        self.console.print(
            f"客户端{client_id}: 实时展示 {state_label}{vanish_hint}{extra}"
        )

    def _relay_server_action(
        self,
        message: dict[str, Any],
        raw_message: str,
        *,
        client_id: int,
    ) -> bool:
        """Relay control actions originated from WebUI server connections."""

        action = message.get("action")
        if not isinstance(action, str):
            return False

        if action == "ping":
            return True

        forwarding_map = {
            "message_data": "推送消息",
            "echo_next": "触发下一条消息",
            "set_live_display": "更新实时展示状态",
            "history_clear": "清空历史记录",
            "set_theme": "设置主题",
            "set_theme_style_url": "设置主题样式",
            "set_avatar": "设置角色头像",
            "broadcast_close": "关闭广播",
            "websocket_close": "关闭 WebSocket 连接",
            "shutdown": "触发关机流程",
        }

        description = forwarding_map.get(action)
        if description is None:
            return False

        origin = message.get("from") or {}
        label = origin.get("name") or origin.get("uuid") or f"客户端{client_id}"

        note = description
        if action == "message_data":
            preview = self._summarize_message_payload(message.get("data"))
            if preview:
                note = f"推送消息: {preview}"

        self.console.print(f"{label}: {note}")

        event_description = f"来自 WebUI: {description}"
        if action == "message_data" and note != description:
            event_description = f"来自 WebUI: {note}"

        self._enqueue_payload(
            raw_message,
            label=action,
            description=event_description,
        )
        return True

    @staticmethod
    def _summarize_message_payload(payload: dict[str, Any] | None) -> str:
        """Generate a short preview string for message_data logs."""

        if not isinstance(payload, dict):
            return ""
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return ""
        first = messages[0]
        text: Any
        if isinstance(first, dict):
            text = first.get("message")
            if isinstance(text, dict):
                text = text.get("text")
        else:
            text = first

        if not isinstance(text, str):
            return ""

        preview = text.strip()
        if len(preview) > 20:
            preview = preview[:20] + "…"
        return preview

    def _handle_hello_event(
        self,
        origin: dict[str, Any],
        payload: dict[str, Any],
        *,
        client_id: int | None = None,
        channel: str | None = None,
    ) -> Optional[str]:
        """Log hello events consistently across websocket sources."""

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
                self._client_types[client_id] = client_type
            self.console.print(f"{label}: 上线{status_text}")
        elif channel is not None:
            label = client_name or origin.get("uuid") or "匿名客户端"
            channel_label = channel or "global"
            self.console.print(
                f"频道[{channel_label}] {label}: 上线{status_text}"
            )

        return client_name

    def _handle_webui_control(self, message: dict[str, Any], channel: str) -> None:
        """Handle control messages originating from the WebUI broadcast websocket."""

        action = message.get("action")
        origin = message.get("from") or {}
        payload = message.get("data") or {}
        channel_name = channel or "global"

        if action == "hello":
            self._handle_hello_event(origin, payload, channel=channel_name)
        elif action == "close":
            label = origin.get("name") or origin.get("uuid") or "匿名客户端"
            self.console.print(f"频道[{channel_name}] {label}: 离开")
        elif action == "page_hidden":
            label = origin.get("name") or origin.get("uuid") or "匿名客户端"
            self.console.print(f"频道[{channel_name}] {label}: 页面隐藏")
        elif action == "page_visible":
            label = origin.get("name") or origin.get("uuid") or "匿名客户端"
            self.console.print(f"频道[{channel_name}] {label}: 页面可见")
        elif action == "websocket_heartbeat":
            return

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
        self.console.print(
            "[yellow]检测到 Ctrl+C，但当前启用了退出保护；请使用 /nocc 关闭保护或使用 /quit 正常退出。[/yellow]"
        )

    def _handle_keyboard_interrupt(self) -> bool:
        if not self.config.get("inhibit_ctrl_c", True):
            return False

        if self._sigint_suppressed:
            self._sigint_suppressed = False
            return True

        self._warn_ctrl_c_guard()
        return True


__all__ = ["EchoServer"]
