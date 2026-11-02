import json
from typing import Literal

from pydantic import BaseModel, Field

from app.auth.roles import Role
from app.llm.anthropic_adapter import AnthropicAdapter, to_anthropic_messages
from app.llm.client import LLMClient
from app.llm.openai_adapter import to_openai_messages
from app.llm.types import (
    Final,
    LLMError,
    Reset,
    TextDelta,
    UsageContext,
    tool_result,
    tool_results_message,
    user_text,
)
from app.llm.usage import MemoryUsageStore, cost_usd
from app.tools.registry import REGISTRY, ToolRegistry, ToolResult
from app.tools.schema import strict_schema
from tests.conftest import make_loaded
from tests.fakes.llm import FakeAdapter, reply


class Args(BaseModel):
    name: str | None = Field(None, description="tên")
    n: int = 1


def test_strict_schema_all_required_no_extra() -> None:
    s = strict_schema(Args)
    assert s["additionalProperties"] is False and sorted(s["required"]) == ["n", "name"]
    assert "title" not in s and "default" not in json.dumps(s)


def test_builtin_tools_role_visibility() -> None:
    import app.tools.builtin  # noqa: F401

    guest = {t.name for t in REGISTRY.for_role(Role.GUEST)}
    forms = {
        "get_form_template",
        "start_form",
        "update_form_fields",
        "attach_file",
        "submit_filled_form",
        "render_preview",
        "cancel_form",
    }
    assert guest == {"search_kb", "list_forms", "get_application_status", *forms}
    assert "return_equipment" not in guest and "request_member_change" not in guest
    assert "request_member_change" not in {t.name for t in REGISTRY.for_role(Role.MEMBER)}
    assert {"return_equipment", "extend_borrow"} <= {t.name for t in REGISTRY.for_role(Role.MEMBER)}
    assert {"return_equipment", "my_borrows"} <= {t.name for t in REGISTRY.for_role(Role.ADMIN)}  # C1
    assert "request_member_change" not in {t.name for t in REGISTRY.for_role(Role.LEADER)}  # C8
    assert "request_member_change" in {t.name for t in REGISTRY.for_role(Role.ADMIN)}
    member = {t.name for t in REGISTRY.for_role(Role.MEMBER)}
    assert {"query_equipment", "my_borrows", "query_duty", "query_members"} <= member
    assert "get_application_status" not in member and "list_pending_applications" not in member
    assert "list_pending_applications" in {t.name for t in REGISTRY.for_role(Role.LEADER)}

    def strict_ok(node: object) -> None:  # mọi object lồng nhau cũng phải strict-compatible
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and set(node["required"]) == set(
                    node["properties"]
                )
            for v in node.values():
                strict_ok(v)
        elif isinstance(node, list):
            for v in node:
                strict_ok(v)

    for t in REGISTRY.for_role(Role.LEADER):
        strict_ok(t.spec().input_schema)


async def test_registry_denies_and_validates() -> None:
    reg = ToolRegistry()

    @reg.tool("secret", "x", {Role.LEADER}, Args)
    async def secret(svc, ctx, a):  # type: ignore[no-untyped-def]
        return ToolResult({"ok": a.n})

    from app.auth.roles import UserContext

    guest = UserContext(Role.GUEST, "s" * 32)
    leader = UserContext(Role.LEADER, "s" * 32)
    assert (await reg.execute("secret", {"name": None, "n": 2}, guest, None)).is_error  # type: ignore[arg-type]
    ok = await reg.execute("secret", {"name": None, "n": 2}, leader, None)  # type: ignore[arg-type]
    assert not ok.is_error and ok.for_llm == {"ok": 2}
    bad = await reg.execute("secret", {"n": "abc"}, leader, None)  # type: ignore[arg-type]
    assert bad.is_error and "Tham số" in bad.llm_text()
    assert (await reg.execute("nope", {}, leader, None)).is_error  # type: ignore[arg-type]


def test_message_conversion_roundtrip() -> None:
    history = [
        user_text("máy chiếu?"),
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "để mình tra"},
                {
                    "type": "tool_use",
                    "id": "call.1",
                    "name": "query_equipment",
                    "input": {"name": "máy chiếu"},
                },
            ],
        },
        tool_results_message([tool_result("call.1", '{"con_lai": 1}')]),
    ]
    a = to_anthropic_messages(history)
    assert a[1]["content"][1]["id"] == "call_1" and a[2]["content"][0]["tool_use_id"] == "call_1"
    o = to_openai_messages("sys", history)
    assert o[0] == {"role": "system", "content": "sys"}
    assert o[2]["tool_calls"][0]["function"]["name"] == "query_equipment"
    assert o[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"con_lai": 1}'}
    # block raw của Anthropic được gửi lại NGUYÊN VẸN (giữ thinking)
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "x"}]
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": "x"}],
        "raw": {"provider": "anthropic", "model": "m", "content": raw},
    }
    assert to_anthropic_messages([msg])[0]["content"] == raw


def test_anthropic_params_by_capability() -> None:
    cfg = make_loaded().config.llm
    ad = AnthropicAdapter("k", "http://x")
    from app.llm.types import CallOptions, ToolSpec

    tool = ToolSpec(
        "t", "d", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    )
    haiku = ad.build_params(
        "claude-haiku-4-5-20251001",
        cfg.capability("claude-haiku-4-5-20251001"),
        "sys",
        [tool],
        [user_text("hi")],
        CallOptions(1000, effort="low"),
    )
    assert "output_config" not in haiku  # Haiku 4.5 không nhận effort
    assert haiku["tools"][0]["strict"] is True and haiku["tool_choice"] == {"type": "auto"}
    assert haiku["system"][0]["cache_control"] == {"type": "ephemeral"}
    sonnet = ad.build_params(
        "claude-sonnet-5-5",
        cfg.capability("claude-sonnet-5-5"),
        "sys",
        [],
        [user_text("hi")],
        CallOptions(1000, effort="low"),
    )
    assert sonnet["output_config"] == {"effort": "low"} and "tools" not in sonnet


async def test_fallback_cross_provider_once() -> None:
    cfg = make_loaded().config.llm
    a = FakeAdapter("anthropic", [LLMError("unavailable", "down")])
    o = FakeAdapter("openai_compat", [reply("trả lời từ gemini")])
    usage = MemoryUsageStore()
    client = LLMClient(cfg, {"anthropic": a, "openai_compat": o}, usage)
    events = [
        ev
        async for ev in client.stream(
            "member_agent", "s", [], [user_text("hi")], UsageContext("member_agent")
        )
    ]
    final = next(e for e in events if isinstance(e, Final)).response
    assert final.model == cfg.fallback.anthropic and final.degraded
    assert a.calls and o.calls and len(usage.rows) == 1


async def test_fallback_on_refusal_resets_partial_text() -> None:
    cfg = make_loaded().config.llm
    a = FakeAdapter("anthropic", [reply("đang trả lời...", stop="refusal")])
    o = FakeAdapter("openai_compat", [reply("ok")])
    client = LLMClient(cfg, {"anthropic": a, "openai_compat": o})
    events = [ev async for ev in client.stream("member_agent", "s", [], [user_text("hi")], UsageContext("x"))]
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TextDelta", "Reset", "TextDelta", "Final"]
    assert isinstance(events[1], Reset) and isinstance(events[2], TextDelta)


async def test_no_fallback_on_bad_request() -> None:
    cfg = make_loaded().config.llm
    a = FakeAdapter("anthropic", [LLMError("bad_request", "400")])
    o = FakeAdapter("openai_compat", [reply("x")])
    client = LLMClient(cfg, {"anthropic": a, "openai_compat": o})
    try:
        [ev async for ev in client.stream("member_agent", "s", [], [user_text("hi")], UsageContext("x"))]
        raise AssertionError("phải ném lỗi")
    except LLMError as e:
        assert e.kind == "bad_request"
    assert not o.calls


async def test_structured_retries_with_tool_result() -> None:
    cfg = make_loaded().config.llm

    class Out(BaseModel):
        so_luong: int

    a = FakeAdapter(
        "anthropic",
        [
            reply(tool_calls=[("submit_result", {"so_luong": "nhiều"})]),
            reply(tool_calls=[("submit_result", {"so_luong": 2})]),
        ],
    )
    client = LLMClient(cfg, {"anthropic": a, "openai_compat": FakeAdapter("openai_compat")})
    out = await client.structured("extract", "s", "trích", Out, UsageContext("extract"))
    assert out.so_luong == 2  # type: ignore[attr-defined]
    second = a.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result" and second[-1]["content"][0]["is_error"]


def test_cost() -> None:
    cfg = make_loaded().config.llm
    from app.llm.types import Usage

    c = cost_usd(cfg.capability("claude-haiku-4-5-20251001"), Usage(6000, 400, 0, 0))
    assert abs(c - 0.008) < 1e-9


def test_loose_schema_for_openai_compat() -> None:
    from app.llm.openai_adapter import loose_schema

    s = loose_schema(strict_schema(Args))
    assert s["required"] == ["n"] and s["properties"]["name"]["type"] == "string"
    assert (
        "anyOf" not in json.dumps(s)
        and "additionalProperties" not in json.dumps(s)
        and "_nullable" not in json.dumps(s)
    )


async def test_registry_treats_empty_string_as_none() -> None:
    reg = ToolRegistry()

    class A(BaseModel):
        kind: Literal["a", "b"] | None = None

    @reg.tool("t", "x", {Role.GUEST}, A)
    async def t(svc, ctx, a):  # type: ignore[no-untyped-def]
        return ToolResult({"kind": a.kind})

    from app.auth.roles import UserContext

    res = await reg.execute("t", {"kind": ""}, UserContext(Role.GUEST, "s" * 32), None)  # type: ignore[arg-type]
    assert not res.is_error and res.for_llm == {"kind": None}


async def test_empty_gateway_response_triggers_fallback() -> None:
    cfg = make_loaded().config.llm
    from app.llm.types import Usage

    empty = reply("")
    empty.usage = Usage()
    o = FakeAdapter("openai_compat", [LLMError("unavailable", "rỗng")])
    a = FakeAdapter("anthropic", [reply("claude trả lời")])
    client = LLMClient(cfg, {"anthropic": a, "openai_compat": o})
    events = [ev async for ev in client.stream("guest_agent", "s", [], [user_text("hi")], UsageContext("x"))]
    final = next(e for e in events if isinstance(e, Final)).response
    assert final.text == "claude trả lời" and final.degraded
