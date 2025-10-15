"""Utilities for parsing and rendering Echo messages."""
from __future__ import annotations

import json
import string
from typing import Any, Dict, Iterable, List, Optional

import jieba
from markdown_it import MarkdownIt
from pypinyin import Style, lazy_pinyin

ALPHABETIC = set(string.ascii_letters)
DEFAULT_PRINT_SPEED = 30
SIZE_STEPS = ["extra-small", "small", "middle", "large", "extra-large"]
SIZE_DEFAULT_INDEX = SIZE_STEPS.index("middle")
_MARKDOWN = MarkdownIt("commonmark")

TYPEWRITING_SCHEMES = {"pinyin", "zhuyin"}
DEFAULT_TYPEWRITING_SCHEME = "pinyin"


def _format_username(config: Dict[str, Any]) -> str:
    raw = config.get("username", "/")
    username = "/" if raw is None else str(raw)
    if not config.get("username_brackets", False):
        return username

    inner = username.strip()
    if inner.startswith("【") and inner.endswith("】") and len(inner) >= 2:
        return inner
    if inner:
        return f"【{inner}】"
    return "【】"


def normalize_typewriting_scheme(scheme: str | None) -> str:
    normalized = (scheme or DEFAULT_TYPEWRITING_SCHEME).strip().lower()
    if normalized in TYPEWRITING_SCHEMES:
        return normalized
    return DEFAULT_TYPEWRITING_SCHEME


def _typewriting_pinyin(text: str) -> str:
    result: List[str] = []
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


def _typewriting_zhuyin(text: str) -> str:
    result: List[str] = []
    for char in text:
        if char in ALPHABETIC:
            result.append(char)
            continue
        reading = lazy_pinyin(char, style=Style.BOPOMOFO)
        if reading:
            syl = reading[0]
            if isinstance(syl, str) and syl:
                result.append(syl)
    return "".join(result)


def get_typewriting_string(text: str, scheme: str | None = None) -> str:
    """Return a phonetic representation for the typewriting effect."""
    normalized = normalize_typewriting_scheme(scheme)
    if normalized == "zhuyin":
        return _typewriting_zhuyin(text)
    return _typewriting_pinyin(text)


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


def _size_index_from_style(style: Dict[str, Any]) -> int:
    size = style.get("size")
    if isinstance(size, str):
        try:
            return SIZE_STEPS.index(size)
        except ValueError:
            pass
    return SIZE_DEFAULT_INDEX


def _apply_fast_formatting(text: str, base_style: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not text:
        return []

    current_style = base_style.copy()
    size_index = _size_index_from_style(current_style)
    active_classes: List[str] = []
    buffer: List[str] = []
    segments: List[Dict[str, Any]] = []

    def push_buffer() -> None:
        if not buffer:
            return
        entry_style = current_style.copy()
        entry: Dict[str, Any] = {
            "text": "".join(buffer),
            "style": entry_style,
        }
        if active_classes:
            entry["class"] = active_classes.copy()
        segments.append(entry)
        buffer.clear()

    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length and text[index + 1] == "@":
            buffer.append("@")
            index += 2
            continue
        if char != "@":
            buffer.append(char)
            index += 1
            continue

        if index + 1 >= length:
            buffer.append("@")
            index += 1
            continue

        code = text[index + 1]
        handled = True

        if code in {"b", "i", "u", "s"}:
            push_buffer()
            if code == "b":
                current_style["bold"] = True
            elif code == "i":
                current_style["italic"] = True
            elif code == "u":
                current_style["underline"] = True
            elif code == "s":
                current_style["strikethrough"] = True
            index += 2
        elif code in {"+", "-"}:
            push_buffer()
            delta = 1 if code == "+" else -1
            size_index = max(0, min(len(SIZE_STEPS) - 1, size_index + delta))
            if size_index == SIZE_DEFAULT_INDEX:
                current_style.pop("size", None)
            else:
                current_style["size"] = SIZE_STEPS[size_index]
            index += 2
        elif code == "r":
            push_buffer()
            current_style = base_style.copy()
            size_index = _size_index_from_style(current_style)
            active_classes = []
            index += 2
        elif code == "[":
            closing = text.find("]", index + 2)
            if closing == -1:
                handled = False
            else:
                push_buffer()
                color = text[index + 2 : closing].strip()
                if color:
                    current_style["color"] = color
                else:
                    current_style.pop("color", None)
                index = closing + 1
        elif code == "{":
            closing = text.find("}", index + 2)
            if closing == -1:
                handled = False
            else:
                push_buffer()
                identifier = text[index + 2 : closing].strip()
                if identifier:
                    emoji_entry: Dict[str, Any] = {"emoji": identifier, "text": ""}
                    if current_style:
                        emoji_entry["style"] = current_style.copy()
                    if active_classes:
                        emoji_entry["class"] = active_classes.copy()
                    segments.append(emoji_entry)
                index = closing + 1
        elif code == "<":
            pos = index + 2
            prefixless = False
            if pos < length and text[pos] == ":":
                prefixless = True
                pos += 1
            start = pos
            while pos < length and not text[pos].isspace() and text[pos] != "@":
                pos += 1
            classname = text[start:pos]
            if not classname:
                handled = False
            else:
                resolved = classname if prefixless else f"echo-text-{classname}"
                if resolved not in active_classes:
                    active_classes.append(resolved)
                index = pos
        else:
            handled = False

        if not handled:
            buffer.append("@")
            index += 1
            continue

    push_buffer()

    if not segments:
        return [{"text": text, "style": base_style.copy()}]

    return segments


def _coerce_positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _effective_print_speed(config: Dict[str, Any], message: Dict[str, Any]) -> int:
    data_field = message.get("data")
    speed = None
    if isinstance(data_field, dict):
        speed = _coerce_positive_int(data_field.get("printSpeed"))

    if speed is None:
        speed = _coerce_positive_int(config.get("print_speed"))

    if speed is None:
        speed = DEFAULT_PRINT_SPEED

    return speed


def _ensure_print_speed(config: Dict[str, Any], entry: Dict[str, Any]) -> None:
    text_value = entry.get("text")
    emoji_value = entry.get("emoji")
    if not (isinstance(text_value, str) and text_value) and not emoji_value:
        return

    data_field = entry.get("data")
    if isinstance(data_field, dict) and _coerce_positive_int(data_field.get("printSpeed")):
        return

    speed = _coerce_positive_int(config.get("print_speed"))
    if speed is None:
        speed = DEFAULT_PRINT_SPEED

    new_data = dict(data_field) if isinstance(data_field, dict) else {}
    new_data["printSpeed"] = speed
    entry["data"] = new_data


def _clone_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    clone: Dict[str, Any] = {}
    for key, value in entry.items():
        if isinstance(value, dict):
            clone[key] = value.copy()
        elif isinstance(value, list):
            clone[key] = value.copy()
        else:
            clone[key] = value
    return clone


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
    """Convert plain text into the structured message representation."""
    if not message:
        return []

    base_style: Dict[str, Any] = {}
    segments: List[Dict[str, Any]] = []
    for segment in _apply_fast_formatting(message, base_style):
        segments.append(dict(segment))

    return _apply_markdown(segments)


def apply_autopause(config: Dict[str, Any], messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Inject pause markers according to the autopause configuration."""

    result: List[Dict[str, Any]] = []

    if not config.get("autopause"):
        return [_clone_entry(entry) for entry in messages]

    autopause_chars = str(config.get("autopausestr", ""))

    try:
        pause_duration = int(config.get("autopausetime", 0))
    except (TypeError, ValueError):
        pause_duration = 0

    if pause_duration <= 0:
        return [_clone_entry(entry) for entry in messages]

    for entry in messages:
        text = entry.get("text")
        if not isinstance(text, str) or text == "":
            result.append(_clone_entry(entry))
            continue

        buffer: List[str] = []
        length = len(text)

        def flush_buffer() -> None:
            if not buffer:
                return
            chunk = "".join(buffer)
            new_entry = _clone_entry(entry)
            new_entry["text"] = chunk
            result.append(new_entry)
            buffer.clear()

        for index, char in enumerate(text):
            buffer.append(char)
            next_char = text[index + 1] if index + 1 < length else None
            should_pause = (
                autopause_chars
                and char in autopause_chars
                and (next_char is None or next_char not in autopause_chars)
            )
            if should_pause:
                flush_buffer()
                result.append({"text": "", "pause": pause_duration})

        flush_buffer()

    if pause_duration > 0:
        result.append({"text": "", "pause": pause_duration})

    return result


def get_delay(config: Dict[str, Any], messages: Iterable[Dict[str, Any]]) -> int:
    """Estimate the client-side playback delay in milliseconds."""
    delay = 0
    for message in messages:
        speed = _effective_print_speed(config, message)
        text = message.get("text", "")
        for char in text:
            if char in ALPHABETIC:
                delay += speed
            else:
                delay += speed * 2
        pause_duration = message.get("pause", 0)
        if isinstance(pause_duration, int):
            delay += speed * pause_duration * 2
        elif isinstance(pause_duration, float):
            delay += int(speed * pause_duration * 2)
    return delay


def render(config: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    """Serialize the parsed messages into the websocket payload format."""
    payload = []
    typewriting_scheme = normalize_typewriting_scheme(
        str(config.get("typewriting_scheme", DEFAULT_TYPEWRITING_SCHEME))
    )
    username_value = _format_username(config)
    for message in messages:
        data = _clone_entry(message)
        text_value = data.get("text")
        _ensure_print_speed(config, data)

        if config.get("typewriting") and isinstance(text_value, str) and text_value:
            segments = _tokenize_for_typewrite(text_value)
            if len(segments) > 1:
                for segment in segments:
                    seg_entry = _clone_entry({key: value for key, value in data.items() if key != "text"})
                    seg_entry["text"] = segment
                    typewrite_value = get_typewriting_string(segment, typewriting_scheme)
                    if typewrite_value:
                        seg_entry["typewrite"] = typewrite_value
                    _ensure_print_speed(config, seg_entry)
                    payload.append(seg_entry)
                continue
            typewrite_value = get_typewriting_string(text_value, typewriting_scheme)
            if typewrite_value:
                data["typewrite"] = typewrite_value
            if isinstance(data.get("style"), dict):
                data["style"] = data["style"].copy()
        payload.append(data)

    return json.dumps(
        {
            "action": "message_data",
            "data": {
                "username": username_value,
                "messages": [
                    {"message": payload},
                ],
            },
        }
    )


__all__ = [
    "apply_autopause",
    "get_delay",
    "get_typewriting_string",
    "normalize_typewriting_scheme",
    "parse_message",
    "render",
]
