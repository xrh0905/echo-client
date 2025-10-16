"""Main entry point for the echo-client websocket helper."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Optional

import websockets
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.table import Table

from config import load_config, save_config
from message import (
    apply_autopause,
    get_delay,
    normalize_typewriting_scheme,
    parse_message,
    render,
)

PING_PAYLOAD = json.dumps({"action": "ping", "data": {}}, ensure_ascii=False)


CommandHandler = Callable[[list[str]], bool]


@dataclass(frozen=True)
class CommandSpec:
    """Metadata describing an interactive console command."""

    name: str
    aliases: tuple[str, ...]
    handler: CommandHandler
    min_args: int = 0
    max_args: int | None = 0
    description: str = ""
    legacy_aliases: tuple[str, ...] = field(default_factory=tuple)
    status_getter: Callable[["EchoServer"], Optional[str]] | None = None


class EchoServer:
    """Orchestrates the websocket server and console interaction."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.config = load_config(self.console)
        self._events: list[dict[str, Any]] = []
        self._client_ids = itertools.count(1)
        self._server: Any | None = None
        self._input_session: PromptSession | None = None
        self._command_specs: tuple[CommandSpec, ...] = ()
        self._command_registry = self._build_command_registry()
        self._key_bindings = self._build_key_bindings()
        self._heartbeat_counts: dict[int, int] = {}
        self._client_names: dict[int, str] = {}
        self._live_display_visibility: dict[int, bool] = {}
        self._graceful_disconnect_requests: dict[int, bool] = {}
        self._parentheses_once: bool = False
        self._interrupt_warning_shown: bool = False

    def _build_command_registry(self) -> dict[str, CommandSpec]:
        """Create the lookup table that powers console commands."""

        def bool_status(key: str, default: bool = False) -> Callable[["EchoServer"], str]:
            def _status(server: "EchoServer", key: str = key, default: bool = default) -> str:
                return "开启" if bool(server.config.get(key, default)) else "关闭"

            return _status

        def username_status(server: "EchoServer") -> str:
            value = str(server.config.get("username", "Someone")).strip()
            return value or "(空)"

        def speed_status(server: "EchoServer") -> str:
            value = server.config.get("print_speed")
            if isinstance(value, (int, float)):
                return f"{value} ms"
            return "未设置"

        def parentheses_status(server: "EchoServer") -> str:
            base = "开启" if server.config.get("auto_parentheses", False) else "关闭"
            if server._parentheses_once:
                base += "；下一条临时开启"
            return base

        def nocc_status(server: "EchoServer") -> str:
            return "开启" if server.config.get("inhibit_ctrl_c", True) else "关闭"

        def suffix_status(server: "EchoServer") -> str:
            if not server.config.get("auto_suffix", True):
                return "关闭"
            value = str(server.config.get("auto_suffix_value", "喵"))
            display = value if value else "(空)"
            return f"开启（{display}）"

        commands = (
            CommandSpec(
                name="help",
                aliases=("h", "?"),
                handler=self._cmd_help,
                min_args=0,
                max_args=1,
                description="显示可用命令与别名",
            ),
            CommandSpec(
                name="quit",
                aliases=("q", "exit"),
                handler=self._cmd_quit,
                description="关闭服务器",
            ),
            CommandSpec(
                name="name",
                aliases=("ren","rename"),
                handler=self._cmd_rename,
                min_args=1,
                max_args=1,
                description="更新显示名称",
                legacy_aliases=("rename",),
                status_getter=username_status,
            ),
            CommandSpec(
                name="speed",
                aliases=("ps",),
                handler=self._cmd_set_print_speed,
                min_args=1,
                max_args=1,
                description="调整默认打印速度 (毫秒)",
                legacy_aliases=("printspeed", "print-speed"),
                status_getter=speed_status,
            ),
            CommandSpec(
                name="typewrite",
                aliases=("tt",),
                handler=self._cmd_toggle_typewriting,
                description="切换 typewriting 效果",
                legacy_aliases=("toggle-typewriting",),
                status_getter=bool_status("typewriting", False),
            ),
            CommandSpec(
                name="scheme",
                aliases=("ts", "tts"),
                handler=self._cmd_toggle_typewriting_scheme,
                description="在拼音与注音之间切换打字机模式",
                legacy_aliases=("toggle-typewriting-scheme",),
                status_getter=lambda srv: normalize_typewriting_scheme(
                    srv.config.get("typewriting_scheme")
                ),
            ),
            CommandSpec(
                name="autopause",
                aliases=("ta",),
                handler=self._cmd_toggle_autopause,
                description="切换 autopause 模式",
                legacy_aliases=("toggle-autopause",),
                status_getter=bool_status("autopause", False),
            ),
            CommandSpec(
                name="quotes",
                aliases=("tq",),
                handler=self._cmd_toggle_quotes,
                description="切换是否自动为消息添加双引号",
                legacy_aliases=("toggle-quotes",),
                status_getter=bool_status("auto_quotes", True),
            ),
            CommandSpec(
                name="suffix",
                aliases=("tsuf",),
                handler=self._cmd_suffix,
                min_args=0,
                max_args=None,
                description="配置自动结尾字符，省略参数时切换开关，on/off 指定状态，其他内容将作为新的结尾文本",
                status_getter=suffix_status,
            ),
            CommandSpec(
                name="paren",
                aliases=("tp",),
                handler=self._cmd_parentheses,
                max_args=1,
                description="切换圆括号包装，或使用 'once' 对下一条消息生效",
                legacy_aliases=("parentheses",),
                status_getter=parentheses_status,
            ),
            CommandSpec(
                name="brackets",
                aliases=("ub", "tub"),
                handler=self._cmd_toggle_username_brackets,
                description="切换是否使用【】包裹用户名",
                legacy_aliases=("toggle-username-brackets",),
                status_getter=bool_status("username_brackets", False),
            ),
            CommandSpec(
                name="skip",
                aliases=("cancel",),
                handler=self._cmd_skip,
                description="向客户端发送 echo_next 指令",
                legacy_aliases=("next", "tn"),
            ),
            CommandSpec(
                name="nocc",
                aliases=("noc",),
                handler=self._cmd_toggle_interrupt_guard,
                min_args=0,
                max_args=1,
                description="配置 Ctrl+C 退出保护，省略参数时切换开关，可用 on/off 显式设置",
                status_getter=nocc_status,
            ),
            CommandSpec(
                name="source",
                aliases=("src", "load", "script"),
                handler=self._cmd_source,
                min_args=1,
                max_args=1,
                description="从文件执行批量命令",
                legacy_aliases=("s",),
            ),
        )

        self._command_specs = commands

        registry: dict[str, CommandSpec] = {}
        for spec in commands:
            all_aliases = {spec.name, *spec.aliases, *spec.legacy_aliases}
            for alias in all_aliases:
                registry[alias.lower()] = spec
        return registry

    def _persist_config(self) -> None:
        save_config(self.config, self.console)

    async def run(self) -> None:
        """Start the websocket server and the console input loop."""
        host = self.config["host"]
        port = self.config["port"]
        self._server = await websockets.serve(self._handle_client, host, port)

        self.console.print(
            f"[green]已经在 {host}:{port} 监听 websocket 请求，等待 echo 客户端接入...[/green]"
        )
        self.console.print("[blue]tips: 如果没有看到成功的连接请求，可以尝试刷新一下客户端[/blue]")
        self.console.print("[green]用户输入模块加载成功，您现在可以开始输入命令了，客户端连接后会自动执行！[/green]")

        asyncio.create_task(self._run_input_loop())

        await self._server.wait_closed()

    async def shutdown(self) -> None:
        """Stop the websocket server."""
        if self._server is None:
            return
        self.console.print("[yellow]正在关闭服务器……[/yellow]")
        self._server.close()
        await self._server.wait_closed()

    async def _handle_client(self, websocket: Any) -> None:
        client_id = next(self._client_ids)
        self.console.print(f"客户端{client_id}: 已建立连接")
        self._heartbeat_counts[client_id] = 0
        self._client_names[client_id] = f"客户端{client_id}"
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
            self._live_display_visibility.pop(client_id, None)
            graceful = self._graceful_disconnect_requests.pop(client_id, False)
            summary = f"客户端{client_id}: 连接已断开（收到心跳 {heartbeat_count} 次）"
            if client_name and client_name != f"客户端{client_id}":
                summary = (
                    f"客户端{client_id}({client_name}): 连接已断开（收到心跳 {heartbeat_count} 次）"
                )
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
                    client_name = origin.get("name") or origin.get("uuid")
                    if client_name:
                        self._client_names[client_id] = client_name
                        self.console.print(f"客户端{client_id}({client_name}): 上线")
                    else:
                        self.console.print(f"客户端{client_id}: 上线")
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
        self._input_session = self._input_session or PromptSession(
            key_bindings=self._key_bindings
        )

        while True:
            try:
                with patch_stdout(raw=True):
                    command = await self._input_session.prompt_async("请输入命令: ")
                if not self._handle_console_command(command.strip()):
                    await self.shutdown()
                    break
            except EOFError:
                await self.shutdown()
                break
            except KeyboardInterrupt:
                if self.config.get("inhibit_ctrl_c", True):
                    if not self._interrupt_warning_shown:
                        self.console.print(
                            "[yellow]检测到 Ctrl+C，但当前启用了退出保护；请使用 /nocc 关闭保护或使用 /quit 正常退出。[/yellow]"
                        )
                        self._interrupt_warning_shown = True
                    else:
                        self.console.print(
                            "[yellow]退出保护仍然开启，如需退出请使用 /quit 或 /nocc 关闭后再尝试 Ctrl+C。[/yellow]"
                        )
                    continue
                await self.shutdown()
                break

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

        spec = self._command_registry.get(action)
        if spec is not None:
            return self._run_command(spec, args)

        self.console.print("[red]这个命令怕是不存在吧……[/red]")
        suggestions = self._suggest_commands(action)
        if suggestions:
            self.console.print(
                "[blue]你是想输入 {} 吗？[/blue]".format(
                    "、".join(suggestions)
                )
            )
        else:
            self.console.print("[blue]tips: 如果你想要发消息，请不要用 '/' 开头！[/blue]")
        prefix = self.config["command_prefix"]
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
        state_label = "开启" if new_state else "关闭"
        self._interrupt_warning_shown = False
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

    def _cmd_help(self, args: list[str]) -> bool:
        prefix = self.config["command_prefix"]

        if args:
            query = args[0].lower()
            spec = self._command_registry.get(query)
            if spec is None:
                self.console.print("[red]没有找到这个命令。[/red]")
                suggestions = self._suggest_commands(query)
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

        for spec in self._command_specs:
            table.add_row(
                f"{prefix}{spec.name}",
                self._format_aliases(spec.aliases, prefix),
                self._command_status(spec),
                self._argument_hint(spec),
                spec.description or "-",
            )

        self.console.print(table)
        if any(spec.legacy_aliases for spec in self._command_specs):
            self.console.print("[dim]提示: 历史命令仍可用，但推荐优先使用表格中的短别名。[/dim]")
        return True

    @staticmethod
    def _format_aliases(aliases: tuple[str, ...], prefix: str) -> str:
        if not aliases:
            return "-"
        return ", ".join(f"{prefix}{alias}" for alias in aliases)

    @staticmethod
    def _argument_hint(spec: CommandSpec) -> str:
        minimum = spec.min_args
        maximum = spec.max_args
        if maximum == 0:
            return "无"
        if maximum is None:
            return f">={minimum}"
        if minimum == 0 and maximum == 1:
            return "可选"
        if minimum == maximum:
            return str(minimum)
        return f"{minimum}-{maximum}"

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

        alias_text = self._format_aliases(spec.aliases, prefix)
        if alias_text != "-":
            self.console.print(f"[white]常用别名[/white]: {alias_text}")

        if spec.legacy_aliases:
            legacy_aliases = ", ".join(f"{prefix}{alias}" for alias in spec.legacy_aliases)
            self.console.print(f"[white]历史别名[/white]: {legacy_aliases}")

        self.console.print(f"[white]参数[/white]: {self._argument_hint(spec)}")
        status = self._command_status(spec)
        if status != "-":
            self.console.print(f"[white]当前值[/white]: {status}")

    def _suggest_commands(self, token: str, limit: int = 3) -> list[str]:
        if not token:
            return []
        candidates = list(self._command_registry.keys())
        matches = get_close_matches(token, candidates, n=limit, cutoff=0.6)
        prefix = self.config["command_prefix"]
        seen: set[str] = set()
        suggestions: list[str] = []
        for match in matches:
            spec = self._command_registry.get(match)
            if spec is None:
                continue
            command_name = f"{prefix}{spec.name}"
            if command_name not in seen:
                suggestions.append(command_name)
                seen.add(command_name)
        return suggestions

    def _command_status(self, spec: CommandSpec) -> str:
        if spec.status_getter is None:
            return "-"
        try:
            status = spec.status_getter(self)
        except Exception as exc:  # pragma: no cover - defensive guard
            self.console.log(f"[red]无法获取 {spec.name} 当前状态: {exc}[/red]")
            return "-"
        if status is None or status == "":
            return "-"
        return str(status)

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

        # one-time flag only applies to the immediate message
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

    def _build_key_bindings(self) -> KeyBindings:
        bindings = {
            "c-b": "@b",
            "c-i": "@i",
            "c-u": "@u",
            "c-d": "@s",
            "c-up": "@+",
            "c-down": "@-",
            "c-space": "@r",
        }

        key_bindings = KeyBindings()

        for key_seq, payload in bindings.items():
            @key_bindings.add(key_seq)
            def _handler(event: KeyPressEvent, payload: str = payload) -> None:
                event.current_buffer.insert_text(payload)

        return key_bindings


def main() -> None:
    console = Console()
    server = EchoServer(console)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        console.print("[yellow]服务器已停止[/yellow]")


if __name__ == "__main__":
    main()
