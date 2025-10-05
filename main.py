"""Main entry point for the echo-client websocket helper."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import websockets
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from config import load_config, save_config
from message import get_delay, parse_message, render

PING_PAYLOAD = json.dumps({"action": "ping", "data": {}}, ensure_ascii=False)


class EchoServer:
    """Orchestrates the websocket server and console interaction."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.config = load_config(self.console)
        self._events: List[dict[str, Any]] = []
        self._client_ids = itertools.count(1)
        self._server: Optional[Any] = None
        self._input_session: Optional[PromptSession] = None
        self._command_handlers = self._build_command_handlers()
        self._heartbeat_counts: Dict[int, int] = {}
        self._client_names: Dict[int, str] = {}

    def _build_command_handlers(self) -> Dict[str, Callable[[List[str]], bool]]:
        return {
            "rename": self._cmd_rename,
            "name": self._cmd_rename,
            "ren": self._cmd_rename,
            "quit": self._cmd_quit,
            "q": self._cmd_quit,
            "source": self._cmd_source,
            "s": self._cmd_source,
            "printspeed": self._cmd_set_print_speed,
            "speed": self._cmd_set_print_speed,
            "ps": self._cmd_set_print_speed,
            "toggle-typewriting": self._cmd_toggle_typewriting,
            "tt": self._cmd_toggle_typewriting,
            "toggle-autopause": self._cmd_toggle_autopause,
            "ta": self._cmd_toggle_autopause,
        }

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
            summary = f"客户端{client_id}: 连接已断开（收到心跳 {heartbeat_count} 次）"
            if client_name and client_name != f"客户端{client_id}":
                summary = (
                    f"客户端{client_id}({client_name}): 连接已断开（收到心跳 {heartbeat_count} 次）"
                )
            if disconnect_reason:
                summary += f"，原因: {disconnect_reason}"
            self.console.print(summary)

    async def _receive_messages(self, websocket: Any, client_id: int) -> None:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                self.console.print(f"客户端{client_id}: 收到无法解析的消息 {message}")
                continue

            action = data.get("action")
            payload = data.get("data", {})
            origin = data.get("from", {})

            if action == "hello":
                client_name = origin.get("name") or origin.get("uuid")
                if client_name:
                    self._client_names[client_id] = client_name
                    self.console.print(f"客户端{client_id}({client_name}): 上线")
                else:
                    self.console.print(f"客户端{client_id}: 上线")
            elif action == "close":
                self.console.print(f"客户端{client_id}: 发出下线请求")
            elif action == "page_hidden":
                self.console.print(f"客户端{client_id}: 页面被隐藏")
            elif action == "page_visible":
                self.console.print(f"客户端{client_id}: 页面恢复显示")
            elif action == "echo_printing":
                username = payload.get("username", "?")
                content = payload.get("message", "") or "(空)"
                if content == "undefined":
                    continue
                self.console.print(f"客户端{client_id}: 正在打印 {username}: {content}")
            elif action == "echo_state_update":
                state = payload.get("state", "unknown")
                remaining = payload.get("messagesCount")
                if state == "ready" and remaining in (0, None):
                    continue
                remaining_str = "未知" if remaining is None else str(remaining)
                self.console.print(f"客户端{client_id}: 状态更新 -> {state}, 剩余消息 {remaining_str}")
            elif action == "error":
                name = payload.get("name", "unknown")
                extras = {k: v for k, v in payload.items() if k != "name"}
                extra_text = f"，详情: {extras}" if extras else ""
                self.console.print(f"[red]客户端{client_id}: 报告错误 {name}{extra_text}[/red]")
            elif action == "websocket_heartbeat":
                self._heartbeat_counts[client_id] = self._heartbeat_counts.get(client_id, 0) + 1
                continue
            else:
                self.console.print(f"客户端{client_id}: 发送了未知事件，事件原文: {data}")

    async def _pump_events(self, websocket: Any, client_id: int) -> None:
        proceed = len(self._events)
        try:
            while True:
                await asyncio.sleep(0.1)
                if proceed >= len(self._events):
                    continue

                for event in self._events[proceed:]:
                    self.console.print(f"客户端{client_id}: 执行 {event['action']}")
                    if event["action"] == "message_data":
                        self.console.print(f"客户端{client_id}: 发送文字信息")
                        await websocket.send(event["data"])
                        await asyncio.sleep(event["delay"] / 1000.0)
                    else:
                        self.console.print(
                            f"[red]客户端{client_id}: 未知事件类型 {event['action']}，已忽略[/red]"
                        )
                proceed = len(self._events)
        except asyncio.CancelledError:
            pass

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
        action = parts[0][len(prefix) :]
        args = parts[1:]

        handler = self._command_handlers.get(action)
        if handler is not None:
            return handler(args)

        self.console.print("[red]这个命令怕是不存在吧……[/red]")
        self.console.print("[blue]tips: 如果你想要发消息，请不要用 '/' 开头！[/blue]")
        return True

    def _cmd_rename(self, args: List[str]) -> bool:
        if len(args) != 1:
            self.console.print("[red]命令接受一个参数，不多不少。[/red]")
            return True
        self.config["username"] = args[0]
        self._persist_config()
        self.console.print(f"[green]已经将显示名称更改为 {args[0]}[/green]")
        return True

    def _cmd_quit(self, args: List[str]) -> bool:
        if args:
            self.console.print("[yellow]提示：退出命令不需要额外参数，参数已忽略。[/yellow]")
        self.console.print("拜拜~")
        return False

    def _cmd_source(self, args: List[str]) -> bool:
        if len(args) != 1:
            self.console.print("[red]命令接受一个参数，不多不少。[/red]")
            return True
        self._execute_source_file(args[0])
        return True

    def _cmd_set_print_speed(self, args: List[str]) -> bool:
        if len(args) != 1:
            self.console.print("[red]命令接受一个参数，不多不少。[/red]")
            return True
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

    def _cmd_toggle_typewriting(self, args: List[str]) -> bool:
        if args:
            self.console.print("[yellow]提示：此命令不接受额外参数，参数已忽略。[/yellow]")
        self.config["typewriting"] = not self.config.get("typewriting", False)
        self._persist_config()
        self.console.print(
            f"[green]Typewriting 状态已经变更为 {self.config['typewriting']}[/green]"
        )
        return True

    def _cmd_toggle_autopause(self, args: List[str]) -> bool:
        if args:
            self.console.print("[yellow]提示：此命令不接受额外参数，参数已忽略。[/yellow]")
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


def main() -> None:
    console = Console()
    server = EchoServer(console)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        console.print("[yellow]服务器已停止[/yellow]")


if __name__ == "__main__":
    main()
