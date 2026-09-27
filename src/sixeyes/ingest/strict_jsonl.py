"""Strict normalized JSONL intake for the owner-facing command.

The legacy parser stays compatible. This boundary refuses unknown fields and coercions
so a provider-native payload cannot be mistaken for fully supported normalized data.
"""
from __future__ import annotations

import json
import math
from typing import Any

from sixeyes.ingest.jsonl import JsonlFormatError, parse_line
from sixeyes.ingest.types import RawRequest, RawTrace


def _check(condition: bool) -> None:
    if not condition:
        raise ValueError("Unsupported normalized trace shape")


def _object(value: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    _check(isinstance(value, dict))
    _check(required <= value.keys() <= allowed)
    return dict(value)


def _array(value: Any) -> list[Any]:
    _check(isinstance(value, list))
    return list(value)


def _string(value: Any) -> None:
    _check(isinstance(value, str))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _check(key not in result)
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    _check(math.isfinite(number))
    return number


def _validate(obj: Any) -> None:
    obj = _object(obj, {"request_id", "timestamp", "model", "system", "tools", "messages", "usage", "tool_choice"},
                  {"request_id", "timestamp", "model", "messages"})
    for field in ("request_id", "model"):
        _string(obj[field])
        _check(bool(obj[field]))
    stamp = obj["timestamp"]
    _check(type(stamp) in (int, float) and math.isfinite(stamp))
    if obj.get("system") is not None:
        _string(obj["system"])
    for tool in _array(obj.get("tools") if obj.get("tools") is not None else []):
        tool = _object(tool, {"name", "description", "schema"}, {"name"})
        _string(tool["name"])
        _check(bool(tool["name"]))
        _string(tool.get("description", ""))
        _check(isinstance(tool.get("schema", {}), dict))
    for msg in _array(obj["messages"]):
        msg = _object(msg, {"role", "content", "tool_call_id", "tool_calls"}, {"role"})
        _check(msg["role"] in ("system", "user", "assistant", "tool"))
        if msg.get("content") is not None:
            _string(msg["content"])
        binding = msg.get("tool_call_id")
        if msg["role"] == "tool":
            _check(isinstance(binding, str) and bool(binding))
        else:
            _check(binding is None)
        calls = _array(msg.get("tool_calls") if msg.get("tool_calls") is not None else [])
        _check(not calls or msg["role"] == "assistant")
        for call in calls:
            call = _object(call, {"id", "name", "arguments"}, {"id", "name", "arguments"})
            for field in ("id", "name", "arguments"):
                _string(call[field])
            _check(bool(call["id"]) and bool(call["name"]))
    usage = obj.get("usage")
    if usage is not None:
        usage = _object(usage, {"input_tokens", "output_tokens", "cache_read_input_tokens"}, set())
        for number in usage.values():
            _check(number is None or (type(number) is int and number >= 0))


def parse_strict_jsonl(text: str, max_requests: int = 1000) -> RawTrace:
    requests: list[RawRequest] = []
    for line_no, line in enumerate(text.removeprefix("\ufeff").split("\n"), 1):
        if not line.strip():
            continue
        try:
            _check(len(requests) < max_requests)
            obj = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_no_constant,
                             parse_float=_finite_float)
            _validate(obj)
            requests.append(parse_line(line, line_no))
        except (ValueError, TypeError, OverflowError, RecursionError):
            # Do not relay parser messages, unknown keys, values or raw text.
            raise JsonlFormatError(line_no, "unsupported normalized JSONL shape or request limit exceeded") from None
    return RawTrace(workload_id="local", requests=tuple(requests))
