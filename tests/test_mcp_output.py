"""Raw MCP output contracts, independent of the corpus and compiler toolchain."""

import json

import anyio
import pytest
from mcp import Client

import reschema.mcp.server as api

TOOLS = [
    ("task_open", {"function": "f"}, "open_function_task"),
    ("experiment", {"function": "f"}, "experiment_function"),
    ("submit_model", {"function": "f", "c_source": "source"}, "submit_function"),
    ("status", {}, "status_snapshot"),
]


def raw_call(tool, arguments):
    async def go():
        # Preserve the initialize/JSON-RPC path used by the existing MCP tests.
        async with Client(api.server, mode="legacy") as client:
            listing = await client.list_tools()
            schema = next(t.output_schema for t in listing.tools if t.name == tool)
            return schema, await client.call_tool(tool, arguments)

    return anyio.run(go)


def assert_object_result(schema, result, payload):
    assert schema is not None and schema["type"] == "object"
    assert result.is_error is False
    # Check the raw channel: conftest.mcp_call's JSON-text fallback would hide
    # missing structured output. Object results must not gain a result wrapper.
    assert result.structured_content == payload
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert result.content[0].text == json.dumps(payload, indent=2, ensure_ascii=False)


@pytest.mark.parametrize("tool,arguments,target", TOOLS)
def test_object_tools_preserve_text_and_publish_structured_content(
    monkeypatch, tool, arguments, target
):
    payload = {"nested": {"values": [None, False, -1]}, "text": "café"}
    monkeypatch.setattr(api, "TaskStore", lambda _: object())
    monkeypatch.setattr(api, target, lambda *a, **kw: payload)
    schema, result = raw_call(tool, {"task_id": "task", **arguments})
    assert_object_result(schema, result, payload)


@pytest.mark.parametrize("accepted", [True, False])
def test_model_judgments_keep_their_payload_and_mcp_success_flag(monkeypatch, accepted):
    payload = (
        {"accepted": True, "task_complete": False}
        if accepted
        else {"accepted": False, "reason": "mismatch", "divergence": {"detail": "ret"}}
    )
    monkeypatch.setattr(api, "TaskStore", lambda _: object())
    monkeypatch.setattr(api, "submit_function", lambda *a, **kw: payload)
    schema, result = raw_call(
        "submit_model", {"task_id": "task", "function": "f", "c_source": "source"}
    )
    assert_object_result(schema, result, payload)


@pytest.mark.parametrize("tool,arguments,target", TOOLS)
def test_unknown_tasks_remain_structured_application_errors(
    monkeypatch, tool, arguments, target
):
    def missing(_):
        raise KeyError("unknown task")

    monkeypatch.setattr(api, "TaskStore", missing)
    schema, result = raw_call(tool, {"task_id": "absent", **arguments})
    assert_object_result(
        schema, result, {"error": "not_found", "detail": "unknown task"}
    )


@pytest.mark.parametrize(
    "tool,arguments,target,error,payload",
    [
        (*TOOLS[1], ValueError("bad spec"), {"error": "spec", "detail": "bad spec"}),
        (
            *TOOLS[3],
            RuntimeError("corrupt ledger"),
            {"error": "internal", "detail": "RuntimeError: corrupt ledger"},
        ),
    ],
)
def test_engine_errors_keep_their_details(
    monkeypatch, tool, arguments, target, error, payload
):
    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(api, "TaskStore", lambda _: object())
    monkeypatch.setattr(api, target, fail)
    schema, result = raw_call(tool, {"task_id": "task", **arguments})
    assert_object_result(schema, result, payload)


def test_invalid_arguments_remain_mcp_errors():
    _, result = raw_call("task_open", {"task_id": []})
    assert result.is_error is True
    assert result.structured_content is None
    assert "task_id" in result.content[0].text
