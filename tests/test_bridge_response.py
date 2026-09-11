"""协议桥接 · 非流式响应侧：Chat Completions JSON -> Responses JSON。"""

from __future__ import annotations

import json

import pytest

from helpers import chat_usage, codex_request

from gateway.bridge import all_tools_of, chat_to_responses, responses_status


def convert(chat: dict, request: dict | None = None) -> dict:
    request = request if request is not None else codex_request()
    return chat_to_responses(chat, request=request, all_tools=all_tools_of(request))


def chat_message(**message) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1757500000,
        "model": "upstream-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": chat_usage(),
    }


def test_shape_and_echoed_request_fields():
    request = codex_request()
    response = convert(chat_message(content="你好"), request)

    assert response["object"] == "response"
    assert response["id"] == "chatcmpl-1", "决策 7：沿用上游 chat 的 id"
    assert response["created_at"] == 1757500000
    assert response["model"] == "upstream-model"
    assert response["status"] == "completed"
    # Codex Desktop 的 "responses lite" 线格式不发顶层 instructions，
    # 系统提示词是 input 里 role=developer 的 message item —— 这里照实回 None
    assert response["instructions"] == request.get("instructions")
    assert response["tool_choice"] == "auto"
    assert response["tools"] == all_tools_of(request)
    assert response["parallel_tool_calls"] is True


def test_text_only_produces_a_single_message_item():
    response = convert(chat_message(content="hi"))
    output = response["output"]

    assert len(output) == 1
    assert output[0]["type"] == "message"
    assert output[0]["role"] == "assistant"
    assert output[0]["status"] == "completed"
    assert output[0]["id"].startswith("msg_")
    assert output[0]["content"] == [{"type": "output_text", "text": "hi", "annotations": []}]


def test_reasoning_item_comes_before_the_message_item():
    """顺序不能动：客户端按 output 的顺序重建下一轮 input。"""
    response = convert(chat_message(content="答案", reasoning_content="先想一想"))
    output = response["output"]

    assert [item["type"] for item in output] == ["reasoning", "message"]
    assert output[0]["id"].startswith("rs_")
    assert output[0]["summary"] == [{"type": "summary_text", "text": "先想一想"}]
    assert output[0]["content"][0]["text"] == "先想一想"


def test_reasoning_is_read_from_either_field_name():
    """各站的思维链字段名不统一：DeepSeek 系叫 reasoning_content，有的站叫 reasoning。"""
    response = convert(chat_message(content="答案", reasoning="换了个名字"))
    assert response["output"][0]["summary"][0]["text"] == "换了个名字"


def test_no_reasoning_means_no_reasoning_item():
    response = convert(chat_message(content="答案"))
    assert [item["type"] for item in response["output"]] == ["message"]


def test_plain_function_call_becomes_function_call_item():
    response = convert(chat_message(
        content=None,
        tool_calls=[{"id": "call_7", "type": "function",
                     "function": {"name": "shell", "arguments": '{"command":["ls"]}'}}],
    ))
    item = response["output"][0]

    assert item["type"] == "function_call"
    assert item["id"] == "fc_call_7", "item id 要带 OpenAI 形状的前缀"
    assert item["call_id"] == "call_7"
    assert item["name"] == "shell"
    assert item["arguments"] == '{"command":["ls"]}'
    assert item["status"] == "completed"


def test_custom_tool_call_is_recognised_and_unwrapped():
    """apply_patch 是 custom 工具：模型回的是 function_call，客户端要的是 custom_tool_call，
    而且 ``input`` 必须是裸 patch 文本，不是包着 content 的 JSON。"""
    patch = "*** Begin Patch\n*** End Patch\n"
    response = convert(chat_message(
        content=None,
        tool_calls=[{"id": "call_8", "type": "function",
                     "function": {"name": "apply_patch",
                                  "arguments": json.dumps({"content": patch}, ensure_ascii=False)}}],
    ))
    item = response["output"][0]

    assert item["type"] == "custom_tool_call"
    assert item["id"] == "ctc_call_8"
    assert item["input"] == patch


def test_custom_tool_names_come_from_additional_tools_too():
    """工具清单必须从 additional_tools 里一起取，否则 apply_patch 会被当成普通函数。

    0.153.4 的线格式把工具包在 namespace 容器里，所以「存在 custom」要往容器里看一层。
    """
    request = codex_request()
    containers = [
        tool["tools"]
        for tool in all_tools_of(request)
        if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list)
    ]
    assert any(t.get("type") == "custom" for tools in containers for t in tools)

    response = convert(
        chat_message(content=None, tool_calls=[{
            "id": "c", "type": "function",
            "function": {"name": "apply_patch", "arguments": '{"content":"x"}'}}]),
        request,
    )
    assert response["output"][0]["type"] == "custom_tool_call"


def test_unparsable_arguments_are_passed_through_untouched():
    """劣质上游会发不是 JSON 的参数：原样交给客户端，别自作主张地「修」。"""
    response = convert(chat_message(
        content=None,
        tool_calls=[{"id": "c", "type": "function",
                     "function": {"name": "shell", "arguments": "not json at all"}}],
    ))
    assert response["output"][0]["arguments"] == "not json at all"


def test_output_order_is_reasoning_message_then_tools():
    response = convert(chat_message(
        content="我来跑一下",
        reasoning_content="需要看目录",
        tool_calls=[
            {"id": "call_a", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
            {"id": "call_b", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
        ],
    ))

    assert [item["type"] for item in response["output"]] == [
        "reasoning", "message", "function_call", "function_call"
    ]


def test_usage_is_mapped_into_responses_fields():
    """Codex 会直接读 input_tokens_details.cached_tokens，那一层不能省。"""
    response = convert(chat_message(content="hi"))

    assert response["usage"] == {
        "input_tokens": 120,
        "input_tokens_details": {"cached_tokens": 80},
        "output_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 150,
    }


def test_missing_usage_falls_back_to_zeros_instead_of_null():
    chat = chat_message(content="hi")
    chat.pop("usage")

    assert convert(chat)["usage"] == {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }


@pytest.mark.parametrize(
    "finish_reason, status",
    [("stop", "completed"), ("tool_calls", "completed"), ("length", "incomplete"),
     ("content_filter", "incomplete"), (None, "completed"), ("something_new", "completed")],
)
def test_status_mapping(finish_reason, status):
    chat = chat_message(content="hi")
    chat["choices"][0]["finish_reason"] = finish_reason

    assert responses_status(finish_reason) == status
    assert convert(chat)["status"] == status


def test_incomplete_response_says_why():
    chat = chat_message(content="hi")
    chat["choices"][0]["finish_reason"] = "length"

    assert convert(chat)["incomplete_details"] == {"reason": "max_output_tokens"}


def test_no_choices_does_not_explode():
    response = convert({"id": "x", "usage": {"prompt_tokens": 1, "completion_tokens": 2}})

    assert response["output"] == []
    assert response["status"] == "completed"
    assert response["usage"]["total_tokens"] == 3
