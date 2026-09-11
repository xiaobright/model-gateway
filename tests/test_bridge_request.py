"""协议桥接 · 请求侧：Responses 请求 -> Chat Completions 请求。

语料是 Codex Desktop 的真实请求形状（tests/fixture/codex_responses_request.json，
键集合与 item 类型照抄 data/captured_request_shape.json 的真实抓包）。
"""

from __future__ import annotations

import json

import pytest

from helpers import codex_request

from gateway.bridge import BridgeError, all_tools_of, responses_to_chat, strip_internal_fields
from gateway.bridge import request as request_mod
from gateway.bridge import tools as tools_mod


def bridge(payload: dict, model: str = "upstream-model") -> dict:
    """转换并摘掉只给日志看的内部字段 —— 断言的对象就是发给上游的那份。"""
    return strip_internal_fields(responses_to_chat(payload, model))


def test_codex_fixture_hoists_additional_tools_and_keeps_function_shape():
    """Codex Desktop 把工具放在 input 的 additional_tools 里，顶层 tools 是空的。

    这是整个桥接的入口条件：漏了这一步，发出去的请求一个工具都没有，
    模型永远不会调 apply_patch，Codex 那边表现为「工具凭空消失」。
    """
    body = bridge(codex_request())

    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["shell", "apply_patch"], "shell 原样透传，apply_patch 由 custom 转来"
    shell = body["tools"][0]["function"]
    assert shell["parameters"]["type"] == "object"
    assert shell["strict"] is False
    assert shell["parameters"]["additionalProperties"] is False


def test_custom_tool_becomes_function_with_grammar_in_description():
    """custom 工具在 chat 那边只能表达成带单个 content 参数的工具，grammar 进 description。"""
    body = bridge(codex_request())
    patch_tool = next(t for t in body["tools"] if t["function"]["name"] == "apply_patch")

    parameters = patch_tool["function"]["parameters"]
    assert list(parameters["properties"]) == ["content"]
    assert parameters["required"] == ["content"]
    assert parameters["properties"]["content"]["type"] == "string"
    assert "```lark" in patch_tool["function"]["description"]
    assert "begin_patch" in patch_tool["function"]["description"]


def test_builtin_web_search_tool_is_dropped_but_reported():
    """web_search 是服务端能力，chat 上游表达不了（决策 5）。丢掉要留下痕迹。"""
    payload = codex_request()
    body = responses_to_chat(payload, "m")

    assert all(tool["function"]["name"] != "web_search" for tool in body["tools"])
    assert body["_bridge_dropped_tools"] == ["web_search"]


def test_instructions_and_developer_messages_both_end_up_as_system():
    """有顶层 `instructions` 的客户端（OpenAI SDK / Cursor）和用 `developer` item 的
    （Codex Desktop）混在一块时，两条 system 都要保住、顺序也不能乱。"""
    body = bridge({
        "instructions": "from instructions",
        "input": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "from developer item"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    })

    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("system", "from instructions"),
        ("system", "from developer item"),
        ("user", "hi"),
    ]


def test_developer_role_becomes_system():
    """上游报过 `unknown variant 'developer', expected one of 'system', 'user', 'assistant',
    'tool'` —— 每一个真实 Codex 请求都会挂在这里。

    Codex Desktop 的 "responses lite" 线格式**不发顶层 instructions**（真实抓包的
    top_level_keys 里没有它），系统提示词是 `role: "developer"` 的 message item 发来的。
    `developer` 是 OpenAI 给新模型起的 system 别名，映射过去语义完全一致。
    """
    body = bridge(codex_request())

    first = body["messages"][0]
    assert first["role"] == "system"
    assert first["content"].startswith("You are Codex"), "系统提示词就是这条 developer 消息"
    assert not any(m["role"] == "developer" for m in body["messages"])


def test_unrecognised_roles_are_passed_through_verbatim():
    """有些站本来就认我们没见过的变体（比如 `latest_reminder`），悄悄改掉反而把一个
    能用的请求改坏 —— 认不出来就让它原样过去，在对面响亮地报错。"""
    body = bridge({
        "input": [
            {"type": "message", "role": "latest_reminder",
             "content": [{"type": "input_text", "text": "remember this"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    })

    assert [m["role"] for m in body["messages"]] == ["latest_reminder", "user"]


def test_input_item_without_a_role_defaults_to_user():
    body = bridge({"input": [{"type": "message", "content": [{"type": "input_text", "text": "hi"}]}]})

    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_reasoning_item_is_merged_into_the_following_assistant_message():
    """推理不能当正文发出去（污染提示词），也不能丢（DeepSeek 多轮要求它在场）。"""
    body = bridge(codex_request())

    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    assert assistant["reasoning_content"] == "先看一眼 README 的现状，再改标题。"
    assert assistant["content"] == "我先读一下 README。"
    assert not any(
        "先看一眼 README" in str(m.get("content")) for m in body["messages"]
    ), "推理摘要不该出现在任何一条消息的正文里"


def test_codex_opaque_encrypted_content_is_not_mistaken_for_replayable_blocks():
    """Codex 的 encrypted_content 是 OpenAI 的不透明密文，不是 litellm 自己写的签名块。

    解不出来就只能回放摘要文本；把它当 thinking_blocks 塞回去会把上游毒死。
    """
    payload = codex_request()
    reasoning = next(item for item in payload["input"] if item.get("type") == "reasoning")
    assert "encrypted_content" in reasoning, "语料本身要带着密文，否则这条测试没意义"

    body = bridge(payload)
    assert all("thinking_blocks" not in m for m in body["messages"])


def test_custom_tool_call_history_becomes_wrapped_function_arguments():
    """历史里的 custom_tool_call 参数在 input 里，要包成 ``{"content": ...}`` 才像 function 调用。"""
    body = bridge(codex_request())
    assistant = next(m for m in body["messages"] if m.get("tool_calls"))
    call = assistant["tool_calls"][0]

    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "apply_patch"
    assert json.loads(call["function"]["arguments"])["content"].startswith("*** Begin Patch")


def test_custom_tool_call_output_becomes_a_tool_message_in_order():
    body = bridge(codex_request())
    messages = body["messages"]
    call_index = next(i for i, m in enumerate(messages) if m.get("tool_calls"))

    assert messages[call_index + 1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Success. Updated the following files:\nM README.md\n",
    }


def test_consecutive_function_calls_collapse_into_one_assistant_message():
    """Anthropic 要求所有 tool_use 在同一个 assistant 消息里，紧跟着 tool_result。"""
    payload = {
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"a":1}'},
            {"type": "function_call", "call_id": "c2", "name": "shell", "arguments": '{"b":2}'},
            {"type": "function_call_output", "call_id": "c1", "output": "one"},
            {"type": "function_call_output", "call_id": "c2", "output": "two"},
        ],
        "tools": [{"type": "function", "name": "shell", "parameters": {}}],
    }
    body = bridge(payload)
    messages = body["messages"]

    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool"]
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["c1", "c2"]


def test_plain_string_input_and_instructions_only():
    body = bridge({"instructions": "be nice", "input": "hello"})

    assert body["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.parametrize(
    "tool_choice, expected",
    [
        (None, None),
        ("auto", "auto"),
        ("required", "required"),
        ({"type": "auto"}, "auto"),
        ({"type": "tool"}, "required"),
        ({"type": "function", "name": "shell"}, {"type": "function", "function": {"name": "shell"}}),
        ({"type": "custom", "name": "apply_patch"}, {"type": "function", "function": {"name": "apply_patch"}}),
    ],
)
def test_tool_choice_normalisation(tool_choice, expected):
    payload = {
        "input": "hi",
        "tools": [{"type": "function", "name": "shell", "parameters": {}}],
        "tool_choice": tool_choice,
    }
    body = bridge(payload)

    assert body.get("tool_choice") == expected


def test_dropped_responses_only_fields_never_reach_the_upstream():
    """store / include / previous_response_id / truncation 这些是 Responses 的服务端语义。"""
    payload = codex_request()
    body = bridge(payload)

    for key in ("store", "include", "truncation", "prompt_cache_key", "client_metadata",
                "context_management", "text"):
        assert key not in body, f"{key} 不该出现在 chat 请求体里"


def test_scalars_and_reasoning_effort_are_mapped():
    body = bridge({
        "input": "hi",
        "max_output_tokens": 4096,
        "temperature": 0.2,
        "top_p": 0.9,
        "user": "u-1",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "high", "summary": "detailed"},
    })

    assert body["max_tokens"] == 4096
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9
    assert body["user"] == "u-1"
    assert body["parallel_tool_calls"] is False
    assert body["reasoning_effort"] == "high"


def test_stream_requests_usage_explicitly():
    """不带 stream_options，上游流式不报 usage，response.completed 就是空的。"""
    assert bridge({"input": "hi"})["model"] == "upstream-model"

    streamed = bridge({"input": "hi", "stream": True})
    assert streamed["stream"] is True
    assert streamed["stream_options"] == {"include_usage": True}

    assert "stream_options" not in bridge({"input": "hi"})


def test_text_format_json_schema_becomes_response_format():
    body = bridge({
        "input": "hi",
        "text": {"format": {"type": "json_schema", "name": "Out", "schema": {"type": "object"}, "strict": True}},
    })

    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Out", "schema": {"type": "object"}, "strict": True},
    }


def test_blank_text_format_is_dropped():
    assert "response_format" not in bridge({"input": "hi", "text": {"verbosity": "low", "format": {"type": "text"}}})


def test_previous_response_id_is_refused():
    """有状态的 Responses 语义没法在 chat 上游上模拟（决策 3）：宁可不参与，也不要假装。"""
    with pytest.raises(BridgeError, match="previous_response_id"):
        responses_to_chat({"input": "hi", "previous_response_id": "resp_123"}, "m")


def test_unknown_input_item_type_is_refused_with_the_type_name():
    """认不出来的 item 不能猜形状（决策 6）：猜错会让模型基于错乱的上下文说胡话。"""
    with pytest.raises(BridgeError, match="local_shell_call"):
        responses_to_chat({"input": [{"type": "local_shell_call", "id": "x"}]}, "m")


def test_orphan_tool_output_is_repaired_from_the_in_process_memo():
    """Codex 压缩历史后中间的 function_call 可能整段消失，只剩一条 tool 结果。

    发给 chat 上游就是「没有对应 tool_call 的 role:tool」，DeepSeek 这类站直接 400。
    我们记得自己这轮发出去过什么，就补得回来。
    """
    tools_mod.remember_tool_call("call_zz", "shell", '{"command":["ls"]}')
    body = bridge({
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]},
            {"type": "function_call_output", "call_id": "call_zz", "output": "file.txt"},
        ],
        "tools": [{"type": "function", "name": "shell", "parameters": {}}],
    })

    assert body["messages"][1]["role"] == "assistant"
    assert body["messages"][1]["tool_calls"][0]["id"] == "call_zz"
    assert body["messages"][2]["role"] == "tool"


def test_unrepairable_orphan_tool_output_is_left_alone():
    """补不动就原样留着 —— 上游会报「tool_call_id 不存在」，那比网关悄悄删历史好查。"""
    body = bridge({
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "call_id": "call_unknown", "output": "x"},
        ],
    })

    assert [m["role"] for m in body["messages"]] == ["user", "tool"]


def test_image_parts_are_dropped_and_reported():
    """v1 不做图片输入（方案 §1）。丢掉要留痕，否则「模型答得莫名其妙」无从查起。"""
    payload = {
        "input": [{
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "看这张图"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ],
        }],
    }
    body = responses_to_chat(payload, "m")

    assert body["messages"][0]["content"] == "看这张图"
    assert body["_bridge_dropped_parts"] == ["input_image"]
    assert "input_image" not in json.dumps(strip_internal_fields(body))


def test_additional_tools_are_visible_to_the_custom_name_scanner():
    """响应侧要拿同一份工具清单认出 custom 调用，两边必须用同一个函数取。"""
    tools = all_tools_of(codex_request())

    assert tools_mod.extract_custom_tool_names(tools) == {"apply_patch"}


def test_named_choice_is_echoed_back_in_responses_shape():
    assert tools_mod.tool_choice_for_response({"type": "function", "name": "shell"}) == {
        "type": "function", "name": "shell"
    }
    assert tools_mod.tool_choice_for_response({"type": "custom", "name": "apply_patch"}) == {
        "type": "custom", "name": "apply_patch"
    }
    assert tools_mod.tool_choice_for_response(None) == "auto"


def test_internal_log_fields_are_stripped_before_sending():
    body = responses_to_chat(codex_request(), "m")
    assert any(key.startswith("_") for key in body)

    clean = strip_internal_fields(body)
    assert not any(key.startswith("_") for key in clean)
    assert clean == request_mod.strip_internal_fields(body)


# ---------------------------------------------------------------- namespace 容器
#
# Codex 0.153.4 的 "responses lite" 把**全部**工具包进 `type: "namespace"` 容器
# （普通函数 + custom 都在名为 `functions` 的默认容器里）。不展开 = 上游一个工具
# 都收不到，模型只能说一句话就结束 —— 2026-09-10 真机首跑就是这个现象。


def namespace_tools() -> list[dict]:
    return [
        {"type": "namespace", "name": "functions", "description": "", "tools": [
            {"type": "function", "name": "shell", "description": "Runs a shell command",
             "strict": False, "parameters": {"type": "object", "properties": {}}},
            {"type": "custom", "name": "apply_patch", "description": "Edits files",
             "format": {"type": "text", "syntax": "unified", "definition": "*** Begin Patch"}},
        ]},
        {"type": "namespace", "name": "browser", "description": "Tools in the browser namespace.", "tools": [
            {"type": "function", "name": "open", "description": "Open a page",
             "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}},
            {"type": "custom", "name": "note", "description": "write note"},
        ]},
    ]


def test_namespace_containers_are_flattened_with_model_visible_names():
    """默认容器用裸名，其它容器按 `{ns}__{name}` 限定；容器描述拼进工具描述。"""
    body = bridge({"tools": namespace_tools(), "input": "hi"})

    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["shell", "apply_patch", "browser__open", "browser__note"]

    # custom 工具转 function 的 content 参数、grammar 后缀都还在
    note = body["tools"][3]["function"]
    assert note["description"].startswith("Tools in the browser namespace.")
    assert note["parameters"]["required"] == ["content"]


def test_namespaced_custom_tool_is_still_recognized_as_custom():
    """非默认命名空间里的 custom 工具，响应侧要能认出来（按模型可见名）。"""
    tools = namespace_tools()

    assert tools_mod.extract_custom_tool_names(tools) == {"apply_patch", "browser__note"}


def test_namespaced_call_splits_back_to_bare_name_and_namespace_field():
    """模型喊 `browser__open` -> item 是裸名 + `namespace` 字段（Codex models.rs 的形状）。

    只回传限定名字符串、不带 namespace 字段的话，Codex 按默认命名空间找
    `browser__open` 这个工具，找不到就当未知调用丢掉。
    """
    from gateway.bridge import chat_to_responses

    tools = namespace_tools()
    response = chat_to_responses(
        {"id": "x", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "call_a", "type": "function",
                 "function": {"name": "browser__open", "arguments": '{"url":"https://x"}'}},
                {"id": "call_b", "type": "function",
                 "function": {"name": "shell", "arguments": "{}"}},
                {"id": "call_c", "type": "function",
                 "function": {"name": "browser__note", "arguments": '{"content":"abc"}'}},
            ]}}]},
        request={"tools": tools, "input": "hi"}, all_tools=tools,
    )

    by_name = {item["name"]: item for item in response["output"] if item["type"].endswith("_call")}
    assert by_name["open"]["namespace"] == "browser"
    assert by_name["open"]["type"] == "function_call"
    assert "namespace" not in by_name["shell"], "默认命名空间不带字段"
    assert by_name["note"]["type"] == "custom_tool_call"
    assert by_name["note"]["namespace"] == "browser"
    assert by_name["note"]["input"] == "abc"


def test_namespaced_history_replay_requalifies_the_name():
    """客户端回放的历史是「裸名 + namespace 字段」-> chat 名要重新限定。"""
    replay = {
        "tools": namespace_tools(),
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "call_a", "name": "open",
             "namespace": "browser", "arguments": '{"url":"https://x"}'},
            {"type": "function_call_output", "call_id": "call_a", "output": "ok"},
        ],
    }
    body = bridge(replay)

    called = [m["tool_calls"][0]["function"]["name"] for m in body["messages"] if m.get("tool_calls")]
    assert called == ["browser__open"]


def test_ambiguous_bare_namespaced_tool_name_is_not_guessed():
    """两个命名空间都有同名工具时，裸名不猜 —— 只有限定名能对上。"""
    tools = [
        {"type": "namespace", "name": "browser", "description": "", "tools": [
            {"type": "function", "name": "open", "parameters": {"type": "object", "properties": {}}},
        ]},
        {"type": "namespace", "name": "files", "description": "", "tools": [
            {"type": "function", "name": "open", "parameters": {"type": "object", "properties": {}}},
        ]},
    ]
    ns_map = tools_mod.namespace_name_map(tools)

    assert tools_mod.split_tool_name("files__open", ns_map) == ("open", "files")
    assert tools_mod.split_tool_name("open", ns_map) == ("open", ""), "歧义裸名不归属任何命名空间"
