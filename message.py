"""Utilities for parsing and rendering Echo messages."""
from __future__ import annotations

import json
import string
from typing import Any, Dict, Iterable, List, Tuple

import jieba
from markdown_it import MarkdownIt
from pypinyin import lazy_pinyin

CHAR_PREFIX = "/"
ALPHABETIC = set(string.ascii_letters)
DEFAULT_PRINT_SPEED = 30

SYM_IMMEDIATE = 1
SYM_DEFERRED = 2

COMMAND_TREE: Dict[str, Any] = {
    "b": SYM_DEFERRED,
    "d": SYM_IMMEDIATE,
    "r": SYM_DEFERRED,
    "s": {"h": SYM_IMMEDIATE},
    "c": {"r": SYM_DEFERRED, "b": SYM_DEFERRED},
}

COMMAND_ARGUMENTS: Dict[str, List[str]] = {
    "d": ["int"],
}


_MARKDOWN = MarkdownIt("commonmark")


def _command_node_for(pointer: Dict[str, Any], char: str) -> Any:
    if char not in pointer:
        raise ValueError(f"未知命令: /{char}")
    return pointer[char]


def _node_is_terminal(value: Any) -> bool:
    return isinstance(value, int)


def _command_starts_at(text: str, start: int) -> bool:
    pointer: Dict[str, Any] = COMMAND_TREE
    index = start
    while index < len(text):
        char = text[index]
        if char not in pointer:
            return False
        pointer = pointer[char]
        index += 1
        if _node_is_terminal(pointer):
            return True
    return False


def _consume_int_argument(message: str, index: int) -> Tuple[int, int]:
    if index >= len(message) or not message[index].isdigit():
        raise ValueError("命令需要一个数值参数")

    digits = []
    while index < len(message) and message[index].isdigit():
        digits.append(message[index])
        index += 1

    value = int("".join(digits))

    if index < len(message) and message[index] == "/":
        if not _command_starts_at(message, index + 1):
            index += 1
    return value, index


def get_typewriting_string(text: str) -> str:
    """Return a phonetic representation for the typewriting effect."""
    result = []
    for idx, char in enumerate(text):
        if char in ALPHABETIC:
            result.append(char)
            continue
        if idx != 0 and text[idx - 1] not in ALPHABETIC:
            result.append("'")
        pinyin = lazy_pinyin(char)
        if pinyin:
            result.append(pinyin[0])
    return "".join(result)


def _tokenize_for_typewrite(text: str) -> List[str]:
    if not text:
        return []

    tokens: List[str] = []
    last_end = 0
    for word, start, end in jieba.tokenize(text, mode="default"):
        if start > last_end:
            tokens.append(text[last_end:start])
        tokens.append(word)
        last_end = end
    if last_end < len(text):
        tokens.append(text[last_end:])

    return [token for token in tokens if token]


def _markdown_segments(text: str, base_style: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split *text* into styled segments according to Markdown emphasis."""
    tokens = _MARKDOWN.parseInline(text, {})
    segments: List[Dict[str, Any]] = []

    for token in tokens:
        if token.type != "inline":
            continue
        segments.extend(_walk_markdown_children(token.children or [], base_style))

    if not segments:
        segments.append({"text": text, "style": base_style.copy()})
    return segments


def _walk_markdown_children(children: List[Any], base_style: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    style_stack: List[Dict[str, Any]] = [base_style.copy()]

    for child in children:
        if child.type == "text":
            content = child.content
            if content:
                segments.append({"text": content, "style": style_stack[-1].copy()})
        elif child.type in {"softbreak", "hardbreak"}:
            segments.append({"text": "\n", "style": style_stack[-1].copy()})
        elif child.type == "strong_open":
            new_style = style_stack[-1].copy()
            new_style["bold"] = True
            style_stack.append(new_style)
        elif child.type == "strong_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif child.type == "em_open":
            new_style = style_stack[-1].copy()
            new_style["italic"] = True
            style_stack.append(new_style)
        elif child.type == "em_close":
            if len(style_stack) > 1:
                style_stack.pop()
        elif child.type == "code_inline":
            literal_style = style_stack[-1].copy()
            literal_style["code"] = True
            segments.append({"text": child.content, "style": literal_style})
        elif child.children:
            segments.extend(_walk_markdown_children(child.children, style_stack[-1]))

    return segments


def _apply_markdown(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    expanded: List[Dict[str, Any]] = []
    for entry in results:
        text = entry.get("text")
        if not isinstance(text, str) or text == "":
            expanded.append(entry)
            continue

        base_style = entry.get("style")
        if isinstance(base_style, dict):
            style_template = base_style.copy()
        else:
            style_template = {}

        extra_fields = {key: value for key, value in entry.items() if key not in {"text", "style"}}
        segments = _markdown_segments(text, style_template)

        for segment in segments:
            seg_entry = extra_fields.copy()
            seg_entry["text"] = segment["text"]
            seg_style = segment.get("style", {}).copy()
            if seg_style:
                seg_entry["style"] = seg_style
            elif isinstance(base_style, dict):
                seg_entry["style"] = style_template.copy()
            expanded.append(seg_entry)

    return expanded


def parse_message(message: str) -> List[Dict[str, Any]]:
    """Parse the inline command syntax into a structured representation."""
    style: Dict[str, Any] = {}
    results: List[Dict[str, Any]] = []
    buffer: List[str] = []
    index = 0

    while index < len(message):
        char = message[index]
        if char != CHAR_PREFIX:
            buffer.append(char)
            index += 1
            continue

        index += 1
        pointer: Dict[str, Any] = COMMAND_TREE
        command = []

        while True:
            if index >= len(message):
                raise ValueError("末尾的命令没有完全匹配！")
            node_char = message[index]
            pointer = _command_node_for(pointer, node_char)
            command.append(node_char)
            index += 1
            if _node_is_terminal(pointer):
                break

        command_str = "".join(command)
        args: List[Any] = []
        for arg_type in COMMAND_ARGUMENTS.get(command_str, []):
            if arg_type != "int":
                raise ValueError(f"未实现的参数类型: {arg_type}")
            value, index = _consume_int_argument(message, index)
            args.append(value)

        text_payload = "".join(buffer)
        if command_str == "sh":
            results.append({"text": "", "event": "shout"})

        results.append({"text": text_payload, "style": style.copy()})
        buffer.clear()

        if command_str == "r":
            style.clear()
        elif command_str == "d":
            pause_duration = int(args[0]) if args else 0
            results.append({"text": "", "pause": pause_duration})
        elif command_str == "cr":
            style["color"] = "#ff0000"
        elif command_str == "cb":
            style["color"] = "#66ccff"
        elif command_str == "b":
            style.clear()
            style["bold"] = True

    results.append({"text": "".join(buffer), "style": style.copy()})
    return _apply_markdown(results)


def get_delay(messages: Iterable[Dict[str, Any]]) -> int:
    """Estimate the client-side playback delay in milliseconds."""
    delay = 0
    for message in messages:
        text = message.get("text", "")
        for char in text:
            if char in ALPHABETIC:
                delay += DEFAULT_PRINT_SPEED
            else:
                delay += DEFAULT_PRINT_SPEED * 2
        delay += DEFAULT_PRINT_SPEED * message.get("pause", 0) * 2
    return delay


def render(config: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    """Serialize the parsed messages into the websocket payload format."""
    payload = []
    for message in messages:
        data = dict(message)
        if config.get("typewriting") and message.get("text"):
            segments = _tokenize_for_typewrite(message.get("text", ""))
            if len(segments) > 1:
                for segment in segments:
                    seg_entry = {key: value for key, value in data.items() if key != "text"}
                    if isinstance(seg_entry.get("style"), dict):
                        seg_entry["style"] = seg_entry["style"].copy()
                    seg_entry["text"] = segment
                    typewrite_value = get_typewriting_string(segment)
                    if typewrite_value:
                        seg_entry["typewrite"] = typewrite_value
                    payload.append(seg_entry)
                continue
            typewrite_value = get_typewriting_string(message.get("text", ""))
            if typewrite_value:
                data["typewrite"] = typewrite_value
            if isinstance(data.get("style"), dict):
                data["style"] = data["style"].copy()
        payload.append(data)

    return json.dumps(
        {
            "action": "message_data",
            "data": {
                "username": config.get("username", "/"),
                "messages": [
                    {"message": payload},
                ],
            },
        }
    )


__all__ = [
    "get_delay",
    "get_typewriting_string",
    "parse_message",
    "render",
]
