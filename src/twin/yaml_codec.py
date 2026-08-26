from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from twin.storage.atomic import write_text


def load_yaml(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    index = _next_content_index(lines, 0)
    if index >= len(lines):
        return {}
    parsed, index = _parse_block(lines, index, _line_indent(lines[index]))
    index = _next_content_index(lines, index)
    if index != len(lines) or not isinstance(parsed, dict):
        raise ValueError(f"expected mapping in {path}")
    return parsed


def dump_yaml(path: Path, value: dict[str, object]) -> None:
    write_text(path, _dump_simple_yaml(value))


def _line_indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _ignored_line(raw: str) -> bool:
    stripped = raw.strip()
    return not stripped or stripped.startswith("#")


def _next_content_index(lines: list[str], index: int) -> int:
    while index < len(lines) and _ignored_line(lines[index]):
        index += 1
    return index


def _parse_scalar(raw: str) -> object:
    value = raw.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_parse_scalar(part) for part in inner.split(",")]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def _parse_block_scalar(
    lines: list[str], index: int, indent: int, style: str
) -> tuple[str, int]:
    values: list[str] = []
    block_indent: int | None = None
    while index < len(lines):
        raw = lines[index]
        if not raw.strip():
            values.append("")
            index += 1
            continue
        current = _line_indent(raw)
        if current <= indent:
            break
        block_indent = current if block_indent is None else block_indent
        values.append(raw[block_indent:])
        index += 1
    if style == ">":
        paragraphs: list[str] = []
        current_paragraph: list[str] = []
        for value in values:
            if value:
                current_paragraph.append(value.strip())
            else:
                if current_paragraph:
                    paragraphs.append(" ".join(current_paragraph))
                    current_paragraph = []
                paragraphs.append("")
        if current_paragraph:
            paragraphs.append(" ".join(current_paragraph))
        return "\n".join(paragraphs).rstrip("\n"), index
    return "\n".join(values).rstrip("\n"), index


def _parse_single_quoted(
    lines: list[str], index: int, indent: int, raw_value: str
) -> tuple[str, int]:
    values = [raw_value[1:]]
    while values and not values[-1].endswith("'") and index < len(lines):
        raw = lines[index]
        if raw.strip() and _line_indent(raw) <= indent:
            break
        current = _line_indent(raw)
        values.append(raw[min(current, len(raw)):])
        index += 1
    text = "\n".join(values)
    if text.endswith("'"):
        text = text[:-1]
    return text.replace("''", "'"), index


def _parse_value(
    lines: list[str], index: int, indent: int, raw_value: str
) -> tuple[object, int]:
    value = raw_value.strip()
    if value in {"|", ">"}:
        return _parse_block_scalar(lines, index, indent, value)
    if value.startswith("'") and not (len(value) >= 2 and value.endswith("'")):
        return _parse_single_quoted(lines, index, indent, value)
    if value:
        return _parse_scalar(value), index
    child_index = _next_content_index(lines, index)
    if child_index < len(lines) and _line_indent(lines[child_index]) > indent:
        return _parse_block(lines, child_index, _line_indent(lines[child_index]))
    return None, index


def _parse_mapping(
    lines: list[str], index: int, indent: int
) -> tuple[dict[str, object], int]:
    result: dict[str, object] = {}
    while index < len(lines):
        index = _next_content_index(lines, index)
        if index >= len(lines) or _line_indent(lines[index]) != indent:
            break
        line = lines[index].strip()
        if line.startswith("- "):
            break
        if ":" not in line:
            raise ValueError(f"unsupported yaml line: {line}")
        key, raw_value = line.split(":", 1)
        result[key.strip()], index = _parse_value(lines, index + 1, indent, raw_value)
    return result, index


def _parse_list_item(
    lines: list[str], index: int, indent: int, item: str
) -> tuple[object, int]:
    if not item:
        child_index = _next_content_index(lines, index)
        if child_index < len(lines) and _line_indent(lines[child_index]) > indent:
            return _parse_block(lines, child_index, _line_indent(lines[child_index]))
        return None, index
    if item in {"|", ">"}:
        return _parse_block_scalar(lines, index, indent, item)
    if ":" in item and not item.startswith(("'", '"')):
        key, raw_value = item.split(":", 1)
        value, index = _parse_value(lines, index, indent + 2, raw_value)
        result: dict[str, object] = {key.strip(): value}
        child_index = _next_content_index(lines, index)
        if child_index < len(lines) and _line_indent(lines[child_index]) > indent:
            child, index = _parse_block(lines, child_index, _line_indent(lines[child_index]))
            if isinstance(child, dict):
                result.update(child)
        return result, index
    return _parse_scalar(item), index


def _parse_list(lines: list[str], index: int, indent: int) -> tuple[list[object], int]:
    result: list[object] = []
    while index < len(lines):
        index = _next_content_index(lines, index)
        if index >= len(lines) or _line_indent(lines[index]) != indent:
            break
        line = lines[index].strip()
        if not line.startswith("- "):
            break
        item, index = _parse_list_item(lines, index + 1, indent, line[2:].strip())
        result.append(item)
    return result, index


def _parse_block(lines: list[str], index: int, indent: int) -> tuple[object, int]:
    if lines[index].strip().startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _quote_scalar(value: str) -> str:
    if value == "" or value != value.strip() or any(char in value for char in ":#[]{}\n"):
        return "'" + value.replace("'", "''") + "'"
    return value


def _dump_scalar(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return _quote_scalar(value)
    return _quote_scalar(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _dump_simple_yaml(value: object, indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    if isinstance(value, dict):
        for key, child in value.items():
            if child == []:
                lines.append(f"{prefix}{key}: []")
            elif child == {}:
                lines.append(f"{prefix}{key}: {{}}")
            elif isinstance(child, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dump_simple_yaml(child, indent + 2).rstrip("\n"))
            elif isinstance(child, str) and "\n" in child:
                lines.append(f"{prefix}{key}: |")
                lines.extend(f"{' ' * (indent + 2)}{line}" for line in child.splitlines())
            else:
                lines.append(f"{prefix}{key}: {_dump_scalar(child)}")
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, dict) and child:
                first_key, first_value = next(iter(child.items()))
                if isinstance(first_value, (dict, list)):
                    lines.append(f"{prefix}- {first_key}:")
                    lines.append(_dump_simple_yaml(first_value, indent + 4).rstrip("\n"))
                else:
                    lines.append(f"{prefix}- {first_key}: {_dump_scalar(first_value)}")
                remainder = dict(list(child.items())[1:])
                if remainder:
                    lines.append(_dump_simple_yaml(remainder, indent + 2).rstrip("\n"))
            elif isinstance(child, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_dump_simple_yaml(child, indent + 2).rstrip("\n"))
            else:
                lines.append(f"{prefix}- {_dump_scalar(child)}")
    else:
        lines.append(prefix + _dump_scalar(value))
    return "\n".join(line for line in lines if line) + "\n"
