"""请求映射：Responses API 请求 dict -> Chat Completions 请求 dict。

移植自 litellm v1.102.0（MIT），原文件
``litellm/responses/litellm_completion_transformation/transformation.py``：

- ``transform_responses_api_request_to_chat_completion_request``（:312）
- ``transform_responses_api_input_to_messages``（:405）
- ``_transform_response_input_param_to_chat_completion_message``（:521）
- ``_transform_responses_api_input_item_to_chat_completion_message``（:1236）
- ``_transform_responses_api_function_call_to_chat_completion_message``（:1592）
- ``_transform_responses_api_tool_call_output_to_chat_completion_message``（:1456）
- ``_ensure_tool_results_have_corresponding_tool_calls``（:1113）
- ``_merge_reasoning_only_assistant_messages``（:668）
- ``_merged_trailing_assistant_message``（:800）
- ``_decode_thinking_blocks_from_input_item``（:1383）
- ``transform_instructions_to_system_message``（:1781）
- ``_transform_text_format_to_response_format``（:2727）

litellm 那版是给 ``litellm.completion()`` 用的，所以请求体里混着一堆 litellm 私有
键（``custom_llm_provider`` / ``extra_headers`` / ``litellm_trace_id``）。我们直接
把 dict 发给上游，那些键全部不要。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from . import tools as tools_mod
from .errors import BridgeError

# Responses 的 input item 类型分三类。会话状态的东西认不出来就只能报错 ——
# 猜一个形状塞给上游，模型会基于错乱的上下文说胡话，比 400 难查得多。
_ITEM_MESSAGE: Final = "message"
_ITEM_REASONING: Final = "reasoning"
# 工具调用的历史：assistant 侧的调用记录
_ITEM_TOOL_CALLS: Final = ("function_call", "custom_tool_call")
# 工具调用的结果：user 侧的执行结果
_ITEM_TOOL_OUTPUTS: Final = ("function_call_output", "custom_tool_call_output", "tool_result")
# Codex 的 "responses lite" 线格式把工具定义塞在 input 里而不是顶层 tools
_ITEM_ADDITIONAL_TOOLS: Final = "additional_tools"
# 被动引用一条已存响应里的 item。我们不做 store/previous_response_id（决策 3），
# 所以这种 item 无处可解
_ITEM_REFERENCE: Final = "item_reference"

# text part 的类型名。Responses 里同一段文本在不同位置叫不同名字
_TEXT_PART_TYPES: Final = ("input_text", "output_text", "text", "summary_text")

# Responses 的 role -> Chat Completions 的 role。
#
# **只有 `developer` 需要改名**：它是 OpenAI 给新模型起的 system 别名（o1 之后），
# api.openai.com 两种都收，而各家兼容站基本只认 system/user/assistant/tool 这四个
# 经典角色 —— 原样透传会得到一句
# `unknown variant 'developer', expected one of 'system', 'user', 'assistant', 'tool'`，
# 整个请求 422。Codex Desktop 的 "responses lite" 线格式**不带顶层 instructions**，
# 系统提示词就是这么发过来的，所以不映射的话每一个真实请求都会挂。
#
# 别的 role 一律原样透传（litellm 也是这么做的）。有些站本来就认我们没见过的变体
# （比如 `latest_reminder`），悄悄改掉反而把一个能用的请求改坏 —— 认不出来就让它
# 在对面响亮地报错。
_ROLE_ALIASES: Final = {"developer": "system"}

# 顶层字段只有这些会被搬到 chat 请求上。其余（store / include / truncation /
# prompt_cache_key / safety_identifier / context_management / client_metadata …）
# 要么对 chat 无意义，要么属于 Responses 的服务端语义，一律丢（方案 3.1）。
_CHAT_PASSTHROUGH: Final = ("temperature", "top_p", "user", "parallel_tool_calls")


def _chat_role(role: object) -> str:
    """把一个 input item 的 role 翻成 chat 那边认识的。"""
    if not isinstance(role, str) or not role:
        return "user"
    return _ROLE_ALIASES.get(role, role)


def _content_text(content: object) -> str:
    """把 Responses 的 content（字符串或 part 数组）压成一段文本。

    图片 part 在 v1 不支持（方案 §1「不做」），这里丢掉并把类型报出去 ——
    静默丢会让模型答得莫名其妙，报出来至少日志里查得到。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        if content.get("type") in _TEXT_PART_TYPES:
            text = content.get("text")
            return text if isinstance(text, str) else ""
        return ""
    if not isinstance(content, Sequence):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in _TEXT_PART_TYPES:
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _dropped_part_types(content: object) -> list[str]:
    """content 里被丢掉的 part 类型（图片、文件……），只用于记日志。"""
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return []
    return [
        str(part.get("type") or "?")
        for part in content
        if isinstance(part, Mapping) and part.get("type") not in _TEXT_PART_TYPES
    ]


def _tool_output_text(output: object) -> str:
    """``function_call_output.output`` -> tool 消息的 content 字符串。

    官方给字符串；有些适配器给 ``[{"type":"input_text","text":...}]``。chat 的 tool
    消息 content 只认字符串最省事，实在拼不出来就整段 JSON 化（总比丢了好）。
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes)):
        text = _content_text(output)
        if text:
            return text
        try:
            return json.dumps(output, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(output)
    try:
        return json.dumps(output, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(output)


def _reasoning_text(item: Mapping[str, object]) -> str | None:
    """从 reasoning item 里取摘要文本。

    ``content`` 优先，``summary`` 兜底（litellm :1371）。Codex 只发
    ``summary: [{"type":"summary_text","text":...}]``，少数客户端发 ``content``。
    """
    for key in ("content", "summary"):
        text = _content_text(item.get(key))
        if text.strip():
            return text
    return None


def _decode_thinking_blocks(item: Mapping[str, object]) -> list[dict] | None:
    """解 ``encrypted_content`` 里的签名 thinking blocks（litellm :1383）。

    litellm 自己会给 Anthropic 这类「推理带签名」的上游写一个 JSON 数组进
    ``encrypted_content``，回放时解出来贴回 assistant 消息，上游才能验签。
    **Codex 发的不是这个**：它的 ``encrypted_content`` 是 OpenAI 的不透明密文，
    解出来不是列表，于是这里返回 None，我们只回放摘要文本。
    """
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted.strip():
        return None
    try:
        decoded = json.loads(encrypted)
    except ValueError:
        return None
    if not isinstance(decoded, list):
        return None
    blocks = [
        dict(block)
        for block in decoded
        if isinstance(block, Mapping)
        and (
            (block.get("type") == "thinking" and block.get("signature"))
            or (block.get("type") == "redacted_thinking" and block.get("data"))
        )
    ]
    return blocks or None


def _reasoning_only_message(item: Mapping[str, object]) -> dict | None:
    """reasoning item -> 只带 ``reasoning_content`` 的 assistant 消息。

    推理**不能**当正文发出去（污染提示词），也不能丢（DeepSeek V4 这类站多轮时会
    因为缺少 reasoning_content 直接拒）。所以先立成一条独立 assistant 消息，后面
    有助手消息再并进去（litellm ``_merge_reasoning_only_assistant_messages``）。
    """
    text = _reasoning_text(item)
    blocks = _decode_thinking_blocks(item)
    if not text and not blocks:
        return None
    message: dict[str, object] = {"role": "assistant", "content": None}
    if text:
        message["reasoning_content"] = text
    if blocks:
        message["thinking_blocks"] = blocks
    return message


def _tool_call_message(item: Mapping[str, object], ns_map: Mapping[str, tuple[str, str]]) -> dict:
    """``function_call`` / ``custom_tool_call`` -> 带 tool_calls 的 assistant 消息。

    custom 工具的参数在 ``input``（裸字符串）里，chat 那边要的是 JSON 串，所以
    包一层 ``{"content": ...}``。这条规则和响应侧的 ``unwrap`` 是同一套（互相逆）。
    名字要重新限定（``tools.qualify_tool_name``）：客户端回放的历史里非默认命名空间
    的调用是「裸名 + namespace 字段」，chat 上游认的是我们发出去的那个限定名。
    """
    call_id = str(item.get("call_id") or item.get("id") or "")
    name = tools_mod.qualify_tool_name(str(item.get("name") or ""), item.get("namespace"), ns_map)
    raw_arguments = item.get("arguments")
    if not raw_arguments and item.get("type") == "custom_tool_call":
        raw_input = item.get("input") or ""
        raw_arguments = json.dumps({"content": raw_input}, ensure_ascii=False) if raw_input else ""
    arguments = tools_mod.serialize_tool_call_arguments(raw_arguments)
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _tool_output_message(item: Mapping[str, object]) -> dict | None:
    """工具结果 -> ``role: tool`` 消息。没有 call_id 就没法配对，只能丢。"""
    call_id = str(item.get("call_id") or "")
    if not call_id:
        return None
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _tool_output_text(item.get("output")),
    }


def _item_to_messages(
    item: Mapping[str, object],
    dropped: list[str],
    ns_map: Mapping[str, tuple[str, str]],
) -> list[dict]:
    """单条 input item -> 0..n 条 chat 消息。"""
    kind = item.get("type")
    if kind in _ITEM_TOOL_OUTPUTS:
        message = _tool_output_message(item)
        return [message] if message is not None else []
    if kind in _ITEM_TOOL_CALLS:
        return [_tool_call_message(item, ns_map)]
    if kind == _ITEM_REASONING:
        message = _reasoning_only_message(item)
        return [message] if message is not None else []
    if kind == _ITEM_ADDITIONAL_TOOLS:
        # 已在 _split_additional_tools 里搬走了，走到这里说明是别处的漏网
        return []
    if kind == _ITEM_MESSAGE or kind is None:
        content = item.get("content")
        if content is None:
            return []
        dropped.extend(_dropped_part_types(content))
        return [{"role": _chat_role(item.get("role")), "content": _content_text(content)}]
    raise BridgeError(
        f"Responses input 里有一条 {kind!r} 类型的 item，网关的协议桥接不支持它。"
        "请把它去掉，或者改用原生 Responses 上游。"
    )


def _merge_into_trailing_assistant(messages: list[dict], produced: list[dict]) -> dict | None:
    """把一条 assistant 正文并进前面那条带 tool_calls 的 assistant 消息（litellm :800）。

    DeepSeek / Anthropic 要求 tool 结果紧跟在 tool_calls 消息后面，中间夹一条
    assistant 正文会被拒。
    """
    if not messages or len(produced) != 1:
        return None
    last = messages[-1]
    new = produced[0]
    if last.get("role") != "assistant" or new.get("role") != "assistant":
        return None
    if not last.get("tool_calls") or last.get("content") or new.get("tool_calls"):
        return None
    if new.get("content") is None:
        return None
    return {**last, "content": new["content"]}


def _merge_reasoning_only(messages: list[dict]) -> list[dict]:
    """把独立的 reasoning 消息并进紧随其后的 assistant 消息（litellm :668）。"""
    merged: list[dict] = []
    pending: list[tuple[str | None, list[dict] | None]] = []
    for message in messages:
        is_reasoning_only = (
            message.get("role") == "assistant"
            and message.get("content") is None
            and not message.get("tool_calls")
            and (message.get("reasoning_content") is not None or message.get("thinking_blocks") is not None)
        )
        if is_reasoning_only:
            pending.append((message.get("reasoning_content"), message.get("thinking_blocks")))
            continue
        if pending and message.get("role") == "assistant":
            texts = [text for text, _ in pending if text]
            blocks = [block for _, group in pending for block in (group or [])]
            if texts:
                existing = message.get("reasoning_content")
                message["reasoning_content"] = "\n".join(texts + ([existing] if existing else []))
            if blocks:
                message["thinking_blocks"] = blocks + list(message.get("thinking_blocks") or [])
            pending = []
        elif pending:
            # 后面不是 assistant（比如又来了一条 user）：reasoning 没地方可并，
            # 保留成独立消息，别把它丢了
            merged.extend(_standalone_reasoning(text, blocks) for text, blocks in pending)
            pending = []
        merged.append(message)
    merged.extend(_standalone_reasoning(text, blocks) for text, blocks in pending)
    return merged


def _standalone_reasoning(text: str | None, blocks: list[dict] | None) -> dict:
    message: dict[str, object] = {"role": "assistant", "content": None}
    if text:
        message["reasoning_content"] = text
    if blocks:
        message["thinking_blocks"] = list(blocks)
    return message


def _split_additional_tools(input_value: object) -> tuple[object, list[dict]]:
    """把 input 里的 ``additional_tools`` item 提到顶层 tools。

    Codex Desktop 的 "responses lite" 线格式（请求头
    ``x-openai-internal-codex-responses-lite: true``）**不带顶层 tools**，
    工具定义放在 ``input`` 里，形如
    ``{"type":"additional_tools","role":"developer","tools":[...]}``。
    litellm 只在 bedrock_mantle provider 里做了这个 hoist，通用转换里没有 ——
    照抄 litellm 会得到一条没有任何工具的请求，模型永远不会调 apply_patch。
    """
    if not isinstance(input_value, list):
        return input_value, []
    hoisted: list[dict] = []
    rest: list[object] = []
    for item in input_value:
        if isinstance(item, Mapping) and item.get("type") == _ITEM_ADDITIONAL_TOOLS:
            tools = item.get("tools")
            if isinstance(tools, list):
                hoisted.extend(tool for tool in tools if isinstance(tool, Mapping))
            continue
        rest.append(item)
    return rest, hoisted


def _repair_orphan_tool_outputs(messages: list[dict], tools: list[dict]) -> int:
    """给没有对应 tool_call 的 tool 结果补一条 assistant 调用（litellm :1113）。

    只补得动「我们这轮自己发出去过的」调用（``tools.recall_tool_call`` 的备忘）——
    那本来就是模型真发过的调用，只是被客户端压缩历史时丢掉了，补回来不算编造。

    litellm 只肯把调用并进**前面那条** assistant 消息；历史被压掉之后往往连那条
    assistant 都没了，这时候我们插一条独立的 assistant 消息出来 —— 一条 ``role:tool``
    紧跟在 user 消息后面是非法历史，严格的站（DeepSeek）会整个请求 400。
    """
    if not messages:
        return 0
    non_tool = sum(1 for message in messages if message.get("role") != "tool")
    kept: list[dict] = []
    repaired = 0
    for message in messages:
        if message.get("role") != "tool":
            kept.append(message)
            continue
        call_id = str(message.get("tool_call_id") or "")
        previous = _previous_assistant_index(kept, len(kept))
        if not call_id and previous is not None:
            calls = kept[previous].get("tool_calls") or []
            if calls and isinstance(calls[0], Mapping):
                call_id = str(calls[0].get("id") or "")
                if call_id:
                    message["tool_call_id"] = call_id
        if not call_id:
            # 没有 call_id 的 tool 消息是彻底废的（上游认不出来），但整条历史不能清空
            if non_tool > 0:
                continue
            kept.append(message)
            continue
        if previous is not None:
            existing = {
                str(call.get("id"))
                for call in kept[previous].get("tool_calls") or []
                if isinstance(call, Mapping)
            }
            if call_id in existing:
                kept.append(message)
                continue
        remembered = tools_mod.recall_tool_call(call_id)
        if remembered is not None:
            name, arguments = remembered
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            if previous is not None:
                kept[previous].setdefault("tool_calls", []).append(call)
            else:
                kept.append({"role": "assistant", "content": None, "tool_calls": [call]})
            repaired += 1
        kept.append(message)
    messages[:] = kept
    return repaired


def _previous_assistant_index(messages: list[dict], current: int) -> int | None:
    for index in range(current - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            return index
    return None


def build_messages(
    payload: Mapping[str, object], items: object, tools: list[object]
) -> tuple[list[dict], list[str]]:
    """instructions + input -> chat 的 messages 数组。

    返回 ``(messages, 被丢掉的 part 类型)``。丢弃清单只为日志存在 —— 内容悄悄少了
    一半，模型答得前言不搭后语时，这行日志就是唯一的线索。
    """
    if isinstance(items, Mapping):
        items = [items]
    dropped: list[str] = []
    messages: list[dict] = []

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(items, str):
        messages.append({"role": "user", "content": items})
        return messages, dropped

    existing_call_ids: set[str] = set()
    ns_map = tools_mod.namespace_name_map(tools)
    for raw_item in items or ():
        if not isinstance(raw_item, Mapping):
            raise BridgeError("Responses input 数组里出现了非对象元素")
        produced = _item_to_messages(raw_item, dropped, ns_map)

        if raw_item.get("type") in _ITEM_TOOL_CALLS:
            call_id = str(raw_item.get("call_id") or raw_item.get("id") or "")
            if call_id:
                existing_call_ids.add(call_id)
            # 连续多条工具调用要合成一条 assistant：Anthropic 要求所有 tool_use 在
            # 同一个 assistant 消息里、紧跟着 tool_result
            if messages and messages[-1].get("role") == "assistant" and produced:
                for message in produced:
                    if message.get("role") == "assistant":
                        messages[-1].setdefault("tool_calls", []).extend(message.get("tool_calls") or [])
                continue

        if raw_item.get("type") in _ITEM_TOOL_OUTPUTS:
            if not produced:
                continue
            # 同一个 call_id 的 assistant 包装只留第一次（Codex 的历史里会重复出现）
            deduped: list[dict] = []
            for message in produced:
                if message.get("role") == "assistant":
                    calls = message.get("tool_calls") or []
                    call_id = str(calls[0].get("id") or "") if calls and isinstance(calls[0], Mapping) else ""
                    if call_id and call_id in existing_call_ids:
                        continue
                    if call_id:
                        existing_call_ids.add(call_id)
                deduped.append(message)
            messages.extend(deduped)
            continue

        merged = _merge_into_trailing_assistant(messages, produced)
        if merged is not None:
            messages[-1] = merged
            continue
        messages.extend(produced)

    _repair_orphan_tool_outputs(messages, tools)
    return _merge_reasoning_only(messages), dropped


def all_tools_of(payload: Mapping[str, object]) -> list[object]:
    """顶层 tools + input 里 ``additional_tools`` 带的 tools。

    响应侧要拿它认出哪个工具是 custom 的（custom 调用要还原成 ``custom_tool_call``
    item），所以两边必须用同一份 —— 只在顶层找会漏掉 Codex Desktop 的全部工具。
    """
    tools = payload.get("tools")
    merged: list[object] = list(tools) if isinstance(tools, list) else []
    _rest, hoisted = _split_additional_tools(payload.get("input"))
    merged.extend(hoisted)
    return merged


def _response_format(payload: Mapping[str, object]) -> dict | None:
    """``text.format`` -> ``response_format``（litellm :2727）。"""
    text = payload.get("text")
    if not isinstance(text, Mapping):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, Mapping):
        return None
    kind = fmt.get("type")
    if kind == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": fmt.get("name", "response_schema"),
                "schema": fmt.get("schema", {}),
                "strict": bool(fmt.get("strict", False)),
            },
        }
    if kind == "json_object":
        return {"type": "json_object"}
    return None


def responses_to_chat(payload: Mapping[str, object], model: str) -> dict:
    """把一个 Responses 请求转成 Chat Completions 请求。

    ``model`` 由调用方给（候选的 ``remote_model``）—— 这个函数不认识路由，
    只认识形状，这样测它的时候不必造一整套配置。
    """
    if not isinstance(payload, Mapping):
        raise BridgeError("请求体必须是 JSON 对象")
    if payload.get("previous_response_id"):
        # 决策 3：桥接是有状态的 Responses 语义里最没法模拟的一块。宁可不参与
        # （proxy 在选候选前就会把桥接候选排除掉），也不要假装能处理
        raise BridgeError("开启转换的候选不支持 previous_response_id，请改用原生 Responses 上游")

    items, hoisted = _split_additional_tools(payload.get("input"))
    all_tools: list[object] = list(payload.get("tools") or []) + hoisted
    chat_tools, dropped_tools = tools_mod.tools_for_chat(all_tools)
    messages, dropped_parts = build_messages(payload, items, all_tools)

    body: dict[str, object] = {"model": model, "messages": messages}
    if chat_tools:
        body["tools"] = chat_tools
        tool_choice = tools_mod.tool_choice_for_chat(payload.get("tool_choice"))
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

    for key in _CHAT_PASSTHROUGH:
        if payload.get(key) is not None:
            body[key] = payload[key]

    max_output = payload.get("max_output_tokens")
    if max_output is not None:
        body["max_tokens"] = max_output

    reasoning = payload.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else reasoning
    if isinstance(effort, str) and effort:
        body["reasoning_effort"] = effort

    response_format = _response_format(payload)
    if response_format is not None:
        body["response_format"] = response_format

    if payload.get("stream"):
        body["stream"] = True
        # 不带上它，上游流式不报 usage；response.completed 里没有 usage，Codex 不认
        body["stream_options"] = {"include_usage": True}

    # 日志专用的观测结果，不进上游请求体（proxy 会用 strip_internal_fields 摘掉）
    if dropped_parts:
        body["_bridge_dropped_parts"] = dropped_parts
    if dropped_tools:
        body["_bridge_dropped_tools"] = dropped_tools
    return body


def strip_internal_fields(body: Mapping[str, object]) -> dict:
    """去掉只给日志看的下划线字段，剩下的才是真正发给上游的请求体。"""
    return {key: value for key, value in body.items() if not str(key).startswith("_")}
