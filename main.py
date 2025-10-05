"""Main entry point for the echo-client websocket helper."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import websockets
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from config import load_config, save_config
from message import get_delay, parse_message, render

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


class EchoServer:
    """Orchestrates the websocket server and console interaction."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.config = load_config(self.console)
        self._events: list[dict[str, Any]] = []
        self._client_ids = itertools.count(1)
        self._server: Any | None = None
        self._input_session: PromptSession | None = None
        self._command_registry = self._build_command_registry()
        self._heartbeat_counts: dict[int, int] = {}
        self._client_names: dict[int, str] = {}
        self._live_display_visibility: dict[int, bool] = {}
        self._graceful_disconnect_requests: dict[int, bool] = {}

    def _build_command_registry(self) -> dict[str, CommandSpec]:
        """Create the lookup table that powers console commands."""

        commands = (
            CommandSpec(
                name="rename",
                aliases=("rename", "name", "ren"),
                handler=self._cmd_rename,
                min_args=1,
                max_args=1,
                description="更新显示名称",
            ),
            CommandSpec(
                name="quit",
                aliases=("quit", "q"),
                handler=self._cmd_quit,
                min_args=0,
                max_args=0,
                description="关闭服务器",
            ),
            CommandSpec(
                name="source",
                aliases=("source", "s"),
                handler=self._cmd_source,
                min_args=1,
                max_args=1,
                description="从文件执行批量命令",
            ),
            CommandSpec(
                name="print-speed",
                aliases=("printspeed", "speed", "ps"),
                handler=self._cmd_set_print_speed,
                min_args=1,
                max_args=1,
                description="调整默认打印速度 (毫秒)",
            ),
            CommandSpec(
                name="toggle-typewriting",
                aliases=("toggle-typewriting", "tt"),
                handler=self._cmd_toggle_typewriting,
                min_args=0,
                max_args=0,
                description="切换 typewriting 效果",
            ),
            CommandSpec(
                name="toggle-autopause",
                aliases=("toggle-autopause", "ta"),
                handler=self._cmd_toggle_autopause,
                min_args=0,
                max_args=0,
                description="切换 autopause 模式",
            ),
        )

        registry: dict[str, CommandSpec] = {}
        for spec in commands:
            for alias in spec.aliases:
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
                    self.console.print(f"客户端{client_id}: 执行 {event['action']}")
                    if event["action"] == "message_data":
                        self.console.print(f"客户端{client_id}: 发送文字信息")
                        try:
                            await websocket.send(event["data"])
                        except websockets.exceptions.ConnectionClosedOK:
                            self.console.print(
                                f"客户端{client_id}: 连接已优雅关闭，停止发送事件"
                            )
                            return
                        except websockets.exceptions.ConnectionClosed as exc:
                            self.console.print(
                                f"客户端{client_id}: 无法发送事件，连接已关闭 ({exc.code})"
                            )
                            return
                        await asyncio.sleep(event["delay"] / 1000.0)
                    else:
                        self.console.print(
                            f"[red]客户端{client_id}: 未知事件类型 {event['action']}，已忽略[/red]"
                        )
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
        self._input_session = self._input_session or PromptSession()

        while True:
            try:
                with patch_stdout(raw=True):
                    command = await self._input_session.prompt_async("请输入命令: ")
                if not self._handle_console_command(command.strip()):
                    await self.shutdown()
                    break
            except (EOFError, KeyboardInterrupt):
                await self.shutdown()
                break

    def _handle_console_command(self, command: str) -> bool:
        prefix = self.config["command_prefix"]

        if not command:
            self.console.print("[red]打个字再回车啊宝！[/red]")
            return True

        literal = self._literal_message_from_command(command, prefix)
        if literal is not None:
            processed = self._apply_autopause(literal)
            self.console.print(f"发送文字消息: {processed}")
            self._enqueue_message(processed)
            return True

        if not command.startswith(prefix):
            processed = self._apply_autopause(command)
            self.console.print(f"发送文字消息: {processed}")
            self._enqueue_message(processed)
            return True

        parts = command.split()
        action = parts[0][len(prefix) :].lower()
        args = parts[1:]

        spec = self._command_registry.get(action)
        if spec is not None:
            return self._run_command(spec, args)

        self.console.print("[red]这个命令怕是不存在吧……[/red]")
        self.console.print("[blue]tips: 如果你想要发消息，请不要用 '/' 开头！[/blue]")
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

    def _cmd_toggle_autopause(self, _args: list[str]) -> bool:
        self.config["autopause"] = not self.config.get("autopause", False)
        self._persist_config()
        self.console.print(
            f"[green]autopause 状态已经变更为 {self.config['autopause']}[/green]"
        )
        return True

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

    def _apply_autopause(self, text: str) -> str:
        if not self.config.get("autopause", False):
            return text
        delay_str = f"/d{self.config['autopausetime']}"
        result = []
        for index, char in enumerate(text):
            result.append(char)
            if (
                char in self.config.get("autopausestr", "")
                and index != len(text) - 1
                and text[index + 1] not in self.config.get("autopausestr", "")
            ):
                result.append(delay_str)
        if not text.endswith(delay_str):
            result.append(delay_str)
        return "".join(result)

    def _enqueue_message(self, text: str) -> None:
        try:
            syntax = parse_message(text)
        except ValueError as exc:
            self.console.print(f"[red]{exc} 行内命令不存在！[/red]")
            return
        payload = render(self.config, syntax)
        delay = get_delay(self.config, syntax)
        self._events.append({"action": "message_data", "data": payload, "delay": delay})

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


def main() -> None:
    console = Console()
    server = EchoServer(console)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        console.print("[yellow]服务器已停止[/yellow]")


if __name__ == "__main__":
    main()
