"""Template mail cố định (email_templates/*.txt.j2). Dòng đầu dạng "Subject: ..." là tiêu đề."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2] / "email_templates"

_env = Environment(  # noqa: S701 - mail dạng text thuần, không phải HTML
    loader=FileSystemLoader(str(ROOT)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render(template: str, /, **ctx: Any) -> tuple[str, str]:
    text = _env.get_template(f"{template}.txt.j2").render(**ctx)
    first, _, rest = text.partition("\n")
    if not first.lower().startswith("subject:"):
        raise ValueError(f"Template {template}: dòng đầu phải là 'Subject: ...'")
    return first.split(":", 1)[1].strip(), rest.lstrip("\n")
