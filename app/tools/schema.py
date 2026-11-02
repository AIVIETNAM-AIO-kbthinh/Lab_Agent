"""Pydantic model → JSON schema dùng được với tool `strict` (additionalProperties=false, mọi trường required).

Trường tuỳ chọn được biểu diễn là nullable (anyOf [..., null]) và vẫn nằm trong `required`.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

_DROP = {"title", "default", "examples"}


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return _inline(copy.deepcopy(defs[name]), defs)
        return {k: _inline(v, defs) for k, v in node.items() if k not in _DROP and k != "$defs"}
    if isinstance(node, list):
        return [_inline(x, defs) for x in node]
    return node


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        node = {k: _strictify(v) for k, v in node.items()}
        if node.get("type") == "object" or "properties" in node:
            props = node.setdefault("properties", {})
            node["type"] = "object"
            node["required"] = list(props.keys())
            node["additionalProperties"] = False
        return node
    if isinstance(node, list):
        return [_strictify(x) for x in node]
    return node


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    schema: dict[str, Any] = _strictify(_inline(raw, defs))
    return schema
