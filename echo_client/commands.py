"""Interactive command definitions and helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Callable, Iterable, Optional, Sequence, TYPE_CHECKING

from .message import normalize_typewriting_scheme

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .server import EchoServer

CommandHandler = Callable[[list[str]], bool]
StatusProvider = Callable[["EchoServer"], Optional[str]]


@dataclass(frozen=True)
class CommandSpec:
    """Metadata describing an interactive console command."""

    name: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    min_args: int = 0
    max_args: int | None = 0
    description: str = ""
    legacy_aliases: tuple[str, ...] = field(default_factory=tuple)
    status_getter: StatusProvider | None = None


class CommandCatalog:
    """Container that resolves command specs by alias and offers suggestions."""

    def __init__(self, specs: Sequence[CommandSpec]) -> None:
        self._specs: tuple[CommandSpec, ...] = tuple(specs)
        registry: dict[str, CommandSpec] = {}
        for spec in self._specs:
            for alias in {spec.name, *spec.aliases, *spec.legacy_aliases}:
                registry[alias.lower()] = spec
        self._registry = registry

    @property
    def specs(self) -> tuple[CommandSpec, ...]:
        return self._specs

    def lookup(self, token: str) -> CommandSpec | None:
        return self._registry.get(token.lower())

    def suggest(self, token: str, prefix: str, limit: int = 3) -> list[str]:
        if not token:
            return []
        matches = get_close_matches(token, list(self._registry.keys()), n=limit, cutoff=0.6)
        seen: set[str] = set()
        suggestions: list[str] = []
        for match in matches:
            spec = self._registry.get(match)
            if spec is None:
                continue
            command_name = f"{prefix}{spec.name}"
            if command_name in seen:
                continue
            suggestions.append(command_name)
            seen.add(command_name)
        return suggestions


def format_aliases(aliases: Iterable[str], prefix: str) -> str:
    alias_list = list(aliases)
    if not alias_list:
        return "-"
    return ", ".join(f"{prefix}{alias}" for alias in alias_list)


def argument_hint(spec: CommandSpec) -> str:
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


def command_status(server: "EchoServer", spec: CommandSpec) -> str:
    getter = spec.status_getter
    if getter is None:
        return "-"
    try:
        status = getter(server)
    except Exception:  # pragma: no cover - defensive
        return "-"
    if status is None or status == "":
        return "-"
    return str(status)


def build_command_specs(server: "EchoServer") -> tuple[CommandSpec, ...]:
    """Construct the command specifications bound to *server*."""

    def bool_status(key: str, default: bool = False) -> StatusProvider:
        def _status(srv: "EchoServer", *, _key: str = key, _default: bool = default) -> str:
            return "开启" if bool(srv.config.get(_key, _default)) else "关闭"

        return _status

    def username_status(srv: "EchoServer") -> str:
        value = str(srv.config.get("username", "Someone")).strip()
        return value or "(空)"

    def speed_status(srv: "EchoServer") -> str:
        value = srv.config.get("print_speed")
        if isinstance(value, (int, float)):
            return f"{value} ms"
        return "未设置"

    def parentheses_status(srv: "EchoServer") -> str:
        base = "开启" if srv.config.get("auto_parentheses", False) else "关闭"
        if srv.parentheses_pending:
            base += "；下一条临时开启"
        return base

    def nocc_status(srv: "EchoServer") -> str:
        return "开启" if srv.config.get("inhibit_ctrl_c", True) else "关闭"

    def suffix_status(srv: "EchoServer") -> str:
        if not srv.config.get("auto_suffix", True):
            return "关闭"
        value = str(srv.config.get("auto_suffix_value", "喵"))
        display = value if value else "(空)"
        return f"开启（{display}）"

    return (
        CommandSpec(
            name="help",
            aliases=("h", "?"),
            handler=server._cmd_help,
            min_args=0,
            max_args=1,
            description="显示可用命令与别名",
        ),
        CommandSpec(
            name="quit",
            aliases=("q", "exit"),
            handler=server._cmd_quit,
            description="关闭服务器",
        ),
        CommandSpec(
            name="name",
            aliases=("ren", "rename"),
            handler=server._cmd_rename,
            min_args=1,
            max_args=1,
            description="更新显示名称",
            legacy_aliases=("rename",),
            status_getter=username_status,
        ),
        CommandSpec(
            name="speed",
            aliases=("ps",),
            handler=server._cmd_set_print_speed,
            min_args=1,
            max_args=1,
            description="调整默认打印速度 (毫秒)",
            legacy_aliases=("printspeed", "print-speed"),
            status_getter=speed_status,
        ),
        CommandSpec(
            name="typewrite",
            aliases=("tt",),
            handler=server._cmd_toggle_typewriting,
            description="切换 typewriting 效果",
            legacy_aliases=("toggle-typewriting",),
            status_getter=bool_status("typewriting", False),
        ),
        CommandSpec(
            name="scheme",
            aliases=("ts", "tts"),
            handler=server._cmd_toggle_typewriting_scheme,
            description="在拼音与注音之间切换打字机模式",
            legacy_aliases=("toggle-typewriting-scheme",),
            status_getter=lambda srv: normalize_typewriting_scheme(srv.config.get("typewriting_scheme")),
        ),
        CommandSpec(
            name="autopause",
            aliases=("ta",),
            handler=server._cmd_toggle_autopause,
            description="切换 autopause 模式",
            legacy_aliases=("toggle-autopause",),
            status_getter=bool_status("autopause", False),
        ),
        CommandSpec(
            name="quotes",
            aliases=("tq",),
            handler=server._cmd_toggle_quotes,
            description="切换是否自动为消息添加双引号",
            legacy_aliases=("toggle-quotes",),
            status_getter=bool_status("auto_quotes", True),
        ),
        CommandSpec(
            name="suffix",
            aliases=("tsuf",),
            handler=server._cmd_suffix,
            min_args=0,
            max_args=None,
            description="配置自动结尾字符，省略参数时切换开关，on/off 指定状态，其他内容将作为新的结尾文本",
            status_getter=suffix_status,
        ),
        CommandSpec(
            name="paren",
            aliases=("tp",),
            handler=server._cmd_parentheses,
            max_args=1,
            description="切换圆括号包装，或使用 'once' 对下一条消息生效",
            legacy_aliases=("parentheses",),
            status_getter=parentheses_status,
        ),
        CommandSpec(
            name="brackets",
            aliases=("ub", "tub"),
            handler=server._cmd_toggle_username_brackets,
            description="切换是否使用【】包裹用户名",
            legacy_aliases=("toggle-username-brackets",),
            status_getter=bool_status("username_brackets", False),
        ),
        CommandSpec(
            name="skip",
            aliases=("cancel",),
            handler=server._cmd_skip,
            description="向客户端发送 echo_next 指令",
            legacy_aliases=("next", "tn"),
        ),
        CommandSpec(
            name="nocc",
            aliases=("noc",),
            handler=server._cmd_toggle_interrupt_guard,
            min_args=0,
            max_args=1,
            description="配置 Ctrl+C 退出保护，省略参数时切换开关，可用 on/off 显式设置",
            status_getter=nocc_status,
        ),
        CommandSpec(
            name="source",
            aliases=("src", "load", "script"),
            handler=server._cmd_source,
            min_args=1,
            max_args=1,
            description="从文件执行批量命令",
            legacy_aliases=("s",),
        ),
    )


__all__ = [
    "CommandCatalog",
    "CommandHandler",
    "CommandSpec",
    "argument_hint",
    "build_command_specs",
    "command_status",
    "format_aliases",
]
