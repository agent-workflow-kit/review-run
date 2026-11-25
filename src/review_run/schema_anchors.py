"""Python flavour of the schemas' ECMA patterns, shared by the config and hook validators."""

from __future__ import annotations

from typing import Any


def python_anchors(node: Any) -> Any:
    """Python's ``$`` also matches before a trailing newline (ECMA's does not): a trailing ``$``
    of every pattern becomes ``\\Z`` in the in-memory copy so ``"PATH\\n"`` cannot pass."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                value = _anchor(value)
            elif key == "patternProperties" and isinstance(value, dict):
                value = {_anchor(k): python_anchors(v) for k, v in value.items()}
            else:
                value = python_anchors(value)
            out[key] = value
        return out
    if isinstance(node, list):
        return [python_anchors(item) for item in node]
    return node


def _anchor(pattern: str) -> str:
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        return pattern[:-1] + r"\Z"
    return pattern
