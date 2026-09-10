"""responses-lite 请求规范化（gateway/normalize.py）。

背景见模块 docstring：Codex 的工具藏在 additional_tools + namespace 容器里，
DeepSeek 这类 Responses 兼容上游不认（模型把调用用 DSML 吐在正文里）。
规范化 = 默认容器的工具提升到顶层 tools，其余原样。
"""

from __future__ import annotations

from helpers import MockUpstream, add_route, add_upstream
import json

from gateway import normalize


def _payload(tools_item: dict) -> dict:
    return {
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            tools_item,
        ],
        "stream": False,
    }


def test_default_namespace_container_is_hoisted_to_top_level_tools():
    """默认容器的 function/custom 工具提升到顶层，类型原样保留。"""
    payload = _payload({"type": "additional_tools", "role": "developer", "tools": [
        {"type": "namespace", "name": "functions", "description": "", "tools": [
            {"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}},
            {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
        ]},
    ]})

    normalized, hoisted = normalize.normalize_responses_request(payload)

    assert hoisted == 2
    assert [(t["type"], t["name"]) for t in normalized["tools"]] == [
        ("function", "shell"), ("custom", "apply_patch"),
    ]
    # additional_tools 的内容全被提走了，item 整个从 input 消失
    assert [i.get("type") for i in normalized["input"] if i.get("type") == "additional_tools"] == []
    # 其它 input item 原样保留
    assert normalized["input"][0]["type"] == "message"


def test_partial_container_keeps_a_slim_additional_tools_item():
    """容器里有非默认内容（web_search 等）时，只剩这些的 item 留在 input 里。"""
    web_search = {"type": "web_search"}
    payload = _payload({"type": "additional_tools", "role": "developer", "tools": [
        {"type": "namespace", "name": "functions", "description": "", "tools": [
            {"type": "function", "name": "shell", "parameters": {"type": "object", "properties": {}}},
        ]},
        web_search,
    ]})

    normalized, hoisted = normalize.normalize_responses_request(payload)

    assert hoisted == 1
    leftovers = [i for i in normalized["input"] if i.get("type") == "additional_tools"]
    assert len(leftovers) == 1
    assert leftovers[0]["tools"] == [web_search], "只有提不走的内容留在 item 里"


def test_non_default_namespace_is_left_alone():
    """非默认 namespace 是原生扩展语义，不动 —— 原生支持的上游不受影响。"""
    container = {"type": "namespace", "name": "browser", "tools": [
        {"type": "function", "name": "open", "parameters": {"type": "object", "properties": {}}},
    ]}
    payload = _payload({"type": "additional_tools", "role": "developer", "tools": [container]})

    normalized, hoisted = normalize.normalize_responses_request(payload)

    assert hoisted == 0
    assert normalized is payload, "没提升就没动，调用方继续字节级透传"
    assert "tools" not in normalized


def _codex_payload(**extra) -> dict:
    """Codex Desktop 的真实线格式（照抄 data/captured_request_shape.json 的形状）：
    顶层 tools 为空，工具在 additional_tools item 里包 functions 容器。"""
    return {
        "model": "m",
        "input": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "You are Codex."}]},
            {"type": "additional_tools", "role": "developer", "tools": [
                {"type": "namespace", "name": "functions", "description": "", "tools": [
                    {"type": "function", "name": "shell",
                     "description": "Runs a shell command", "strict": False,
                     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
                    {"type": "custom", "name": "apply_patch", "description": "Edits files",
                     "format": {"type": "text", "syntax": "unified", "definition": "*** Begin Patch"}},
                ]},
                {"type": "web_search"},
            ]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "parallel_tool_calls": False,
        "stream": False,
        "store": False,
        **extra,
    }


def test_real_codex_fixture_normalizes_end_to_end():
    """真实抓包形状：提完之后顶层 tools 就是 shell + apply_patch(custom)。"""
    payload = _codex_payload()
    assert normalize.needs_normalization(payload)

    normalized, hoisted = normalize.normalize_responses_request(payload)

    assert hoisted == 2
    assert [(t["type"], t["name"]) for t in normalized["tools"]] == [
        ("function", "shell"), ("custom", "apply_patch"),
    ]


def test_gateway_forwards_standard_tools_to_native_upstream(gateway):
    """集成：透传给 Responses 上游的请求里，工具在顶层、默认容器已消失。

    这是 DSML 问题的端到端钉子 —— 上游收到的形状必须和「官方 SDK 直连」一致。
    """
    with MockUpstream("siteA") as mock:
        add_upstream(gateway, mock, "siteA", "openai")
        add_route(gateway, "gpt-native", _group_of(gateway, "siteA"), "deepseek-flash")

        resp = gateway.post("/v1/responses", json={**_codex_payload(), "model": "gpt-native"})
        assert resp.status_code == 200

        sent = mock.last_responses_request()
        assert [(t["type"], t["name"]) for t in sent.get("tools", [])] == [
            ("function", "shell"), ("custom", "apply_patch"),
        ]
        leftovers = [i for i in sent["input"] if i.get("type") == "additional_tools"]
        assert all("namespace" not in json.dumps(i) or i.get("tools") for i in leftovers)
        assert not any(
            t.get("type") == "namespace"
            for i in leftovers
            for t in (i.get("tools") or [])
            if i.get("type") == "additional_tools"
        ), "默认容器不能还留在 input 里"
        assert sent["model"] == "deepseek-flash"


def _group_of(gateway, name):
    for upstream in gateway.get("/admin/api/upstreams").json():
        if upstream["name"] == name:
            return upstream["groups"][0]["id"]
    raise AssertionError(f"没有名为 {name} 的上游")
