"""Strict parser for the supported Chat Completions tool-choice subset.

Call only when the field is present. Omission is distinct from explicit modes;
null, allowed-tools lists and custom tools are not supported in this pilot.
Errors contain only static schema text, including for unexpected key names.
"""

from typing import Literal, cast

from sixeyes.ingest.types import RawToolChoice


def parse_tool_choice(value: object) -> RawToolChoice:
    if isinstance(value, str) and value in ("auto", "none", "required"):
        return RawToolChoice(cast(Literal["auto", "none", "required"], value))
    if isinstance(value, dict) and set(value) == {"type", "function"}:
        function = value["function"]
        if value["type"] == "function" and isinstance(function, dict) and set(function) == {"name"}:
            name = function["name"]
            if isinstance(name, str) and name:
                return RawToolChoice("function", name)
    raise ValueError(
        "tool_choice: expected auto, none, required, or a function object with a non-empty name"
    )
