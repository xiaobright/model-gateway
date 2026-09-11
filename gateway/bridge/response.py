"""非流式响应映射：Chat Completions JSON -> Responses JSON。

移植自 litellm v1.102.0（MIT），原文件
``litellm/responses/litellm_completion_transformation/transformation.py``：

- ``transform_chat_completion_response_to_responses_api_response``（:2259）
- ``_transform_chat_completion_choices_to_responses_output``（:2319）
- ``_extract_reasoning_output_items``（:2412）
- ``_extract_message_output_items``（:2539）
- ``transform_chat_completion_tools_to_responses_tools``（:2041）
- ``_map_chat_completion_finish_reason_to_responses_status``（:2131）
- ``_transform_chat_completion_usage_to_responses_usage``（:2657）

litellm 那版用 ``ModelResponse(**dict)`` 把上游 JSON 包成 pydantic 再逐个
``getattr``。我们直接读 dict。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from . import tools as tools_mod

# finish_reason -> Responses 的 status（litellm :2131）。认不出来的一律 completed：
# 上游给的枚举名千奇百怪（有的站甚至回 "stop_reason" 之类），宁可当作正常收尾。
_INCOMPLETE_REASONS: Final = ("length", "content_filter", "refusal")


def responses_status(finish_reason: object) -> str:
    if isinstance(finish_reason, str) and finish_reason in _INCOMPLETE_REASONS:
        return "incomplete"
    return "completed"


def _output_text_item(text: str, annotations: Sequence[object] | None = None) -> dict:
    return {"type": "output_text", "text": text, "annotations": list(annotations or ())}


def _reasoning_item(message: Mapping[str, object], status: str) -> dict | None:
    """``message.reasoning_content`` -> Responses 的 ``reasoning`` item。

    两个字段名都认：DeepSeek 系用 ``reasoning_content``，有些站叫 ``reasoning``
    （方案决策 8）。上游完全没有推理内容时返回 None，正文照旧当纯文本转发。

    ``summary`` 和 ``content`` 都填：Codex 读 ``summary``，而 OpenAI 原生响应里
    ``reasoning`` item 的明文也可能落在 ``content`` 上 —— 两处都填，谁读哪个都不落空。
    """
    text = message.get("reasoning_content") or message.get("reasoning")
    if not isinstance(text, str) or not text:
        text = ""
    blocks = message.get("thinking_blocks")
    encrypted: str | None = None
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes)):
        preserved = [
            dict(block)
            for block in blocks
            if isinstance(block, Mapping) and (block.get("signature") or block.get("data"))
        ]
        if preserved:
            encrypted = json.dumps(preserved, separators=(",", ":"), ensure_ascii=False)
    if not text and not encrypted:
        return None
    item: dict[str, object] = {
        "type": "reasoning",
        "id": f"rs_{uuid.uuid4()}",
        "status": status,
        "role": "assistant",
        "summary": [{"type": "summary_text", "text": text}],
        "content": [_output_text_item(text)],
    }
    if encrypted:
        item["encrypted_content"] = encrypted
    return item


def _message_item(message: Mapping[str, object], status: str) -> dict | None:
    content = message.get("content")
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4()}",
        "status": status,
        "role": "assistant",
        "content": [_output_text_item(content)],
    }


def _tool_call_items(
    message: Mapping[str, object], status: str, custom_names: set[str], ns_map: Mapping[str, tuple[str, str]]
) -> list[dict]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
        return []
    items: list[dict] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        function = function if isinstance(function, Mapping) else {}
        call_id = str(call.get("id") or "")
        name = str(function.get("name") or "")
        arguments = tools_mod.serialize_tool_call_arguments(function.get("arguments"))
        # 模型喊的可能是带命名空间前缀的限定名；拆回「裸名 + namespace 字段」，
        # Codex 才能把调用路由进正确的命名空间（tools.split_tool_name）
        bare, namespace = tools_mod.split_tool_name(name, ns_map)
        is_custom = name in custom_names or bare in custom_names
        items.append(
            tools_mod.build_tool_call_item_kwargs(
                call_id, bare, arguments, "completed", custom_names,
                namespace=namespace, is_custom=is_custom,
            )
        )
        # 记下来给后续轮次修复孤立的 tool 结果用（见 tools.remember_tool_call）；
        # 记 chat 侧的限定名 —— 修复时拼回的也是 chat 消息
        tools_mod.remember_tool_call(call_id, name, arguments)
    return items


def _usage(chat: Mapping[str, object]) -> dict:
    """chat 的 usage -> Responses 的 usage（litellm :2657）。

    Codex 侧最关心的是 ``input_tokens`` 和 ``output_tokens`` 存在且是整数；
    ``cached_tokens`` / ``reasoning_tokens`` 是明细，缺就补 0，别省掉那一层 ——
    Codex 里有代码直接读 ``input_tokens_details.cached_tokens``。
    """
    raw = chat.get("usage")
    usage = raw if isinstance(raw, Mapping) else {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not isinstance(prompt, int):
        prompt = 0
    if not isinstance(completion, int):
        completion = 0
    if not isinstance(total, int):
        total = prompt + completion

    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
    cached = prompt_details.get("cached_tokens")
    input_details: dict[str, int] = {"cached_tokens": cached if isinstance(cached, int) else 0}
    for key in ("text_tokens", "audio_tokens"):
        value = prompt_details.get(key)
        if isinstance(value, int):
            input_details[key] = value

    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, Mapping) else {}
    reasoning_tokens = completion_details.get("reasoning_tokens")
    output_details: dict[str, int] = {
        "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, int) else 0
    }
    for key in ("audio_tokens", "text_tokens", "image_tokens"):
        value = completion_details.get(key)
        if isinstance(value, int):
            output_details[key] = value

    return {
        "input_tokens": prompt,
        "input_tokens_details": input_details,
        "output_tokens": completion,
        "output_tokens_details": output_details,
        "total_tokens": total,
    }


def build_output(
    chat: Mapping[str, object], status: str, custom_names: set[str], ns_map: Mapping[str, tuple[str, str]]
) -> list[dict]:
    """choices[0].message -> Responses 的 output 数组。

    顺序是固定的：reasoning item（有的话）→ message item（有正文的话）→ 每个工具调用
    一个 item。这个顺序不能动 —— 客户端按它重建下一轮的 input。
    """
    choices = chat.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, Mapping):
        return []
    message = first.get("message")
    if not isinstance(message, Mapping):
        return []

    output: list[dict] = []
    reasoning = _reasoning_item(message, status)
    if reasoning is not None:
        output.append(reasoning)
    text = _message_item(message, status)
    if text is not None:
        output.append(text)
    output.extend(_tool_call_items(message, status, custom_names, ns_map))
    return output


def chat_to_responses(
    chat: Mapping[str, object],
    *,
    request: Mapping[str, object],
    all_tools: Sequence[object] | None = None,
) -> dict:
    """一个 Chat Completions 响应 -> Responses 响应。

    ``request`` 是客户端原始请求，只用来回填 Codex 会读的那几个字段（tools /
    tool_choice / temperature…）以及认出哪些工具是 custom 的。
    """
    if not isinstance(chat, Mapping):
        return _error_response("上游返回的不是 JSON 对象")

    choices = chat.get("choices")
    finish_reason: object = None
    if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            finish_reason = first.get("finish_reason")
    status = responses_status(finish_reason)

    custom_names = tools_mod.extract_custom_tool_names(all_tools)
    ns_map = tools_mod.namespace_name_map(all_tools)
    response_id = chat.get("id")
    if not isinstance(response_id, str) or not response_id:
        response_id = f"resp_{uuid.uuid4()}"

    created = chat.get("created")
    if not isinstance(created, int):
        created = int(time.time())
    model = chat.get("model")
    if not isinstance(model, str):
        model = str(request.get("model") or "")

    response: dict[str, object] = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "instructions": request.get("instructions"),
        "model": model,
        "output": build_output(chat, status, custom_names, ns_map),
        "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
        "tool_choice": tools_mod.tool_choice_for_response(request.get("tool_choice")),
        "tools": list(all_tools or ()),
        "top_p": request.get("top_p"),
        "max_output_tokens": request.get("max_output_tokens"),
    }
    if request.get("temperature") is not None:
        response["temperature"] = request["temperature"]
    if request.get("metadata") is not None:
        response["metadata"] = request["metadata"]
    response["usage"] = _usage(chat)
    return response


def _error_response(message: str) -> dict:
    return {
        "id": f"resp_{uuid.uuid4()}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "failed",
        "error": {"code": "bridge_error", "message": message},
        "output": [],
        "usage": _usage({}),
    }
