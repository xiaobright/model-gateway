"""工具与 tool_choice 的双向映射：Responses ↔ Chat Completions。

移植自 litellm v1.102.0（MIT），原文件与函数：

- ``litellm/responses/litellm_completion_transformation/custom_tools.py``
  （``extract_custom_tool_names`` / ``is_custom_tool_call`` /
  ``serialize_tool_call_arguments`` / ``unwrap_custom_tool_arguments`` /
  ``build_tool_call_item_kwargs`` / ``convert_custom_tool_to_function_tool``）
- ``litellm/responses/litellm_completion_transformation/transformation.py``
  ``ResponsesToolChatForm`` + ``_responses_tool_to_chat_form``（:114 / :1870）、
  ``_transform_tool_choice``（:213）、
  ``transform_responses_api_tools_to_chat_completion_tools``（:1945）、
  ``transform_chat_completion_tools_to_responses_tools``（:2041）、
  ``_tool_call_id_from_responses_item``（:2160）

litellm 同时支持 pydantic 对象和 dict，到处是 ``getattr`` / ``_get_mapping_or_attr_value``
的双通道防御。这里只吃 raw dict、只吐 raw dict，那一层防御全部去掉，语义保留。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from .errors import BridgeError

# 工具调用的 output item id 前缀。客户端（Codex）按前缀区分 function_call 和
# custom_tool_call，缺了前缀它会把两种 item 当成同一类。
_ITEM_ID_PREFIX: Final = {"function_call": "fc", "custom_tool_call": "ctc"}

# 参数超过这个长度就不再尝试 json 解包 —— unwrap 只是为了让日志里的 input 好看，
# 一条 1MB 的参数去解包纯属浪费。
_MAX_ARGUMENTS_LEN: Final = 1_000_000

# custom 工具转成 function 工具后，模型必须回一个带 content 字段的 JSON 对象。
# 这个描述会拼进 function 的 description，grammar 定义也拼在它后面。
_CONTENT_PARAM_DESCRIPTION: Final = "The {name} content following the specified format"

# Responses 里只有这三种工具能落到 Chat Completions 上（``namespace`` 是容器，
# 展开后再认里面的 function / custom）。其余（web_search、local_shell、
# computer_use、image_generation、mcp……）要么是服务端能力、要么是另一种线格式
# 的私有形状，chat 上游一律不认，见 tools_for_chat 的注释。
_CONVERTIBLE_TOOL_TYPES: Final = ("function", "custom")

# namespace 容器的展开规则与模型可见名在 gateway/normalize.py 定义（透传层也要用，
# 那边自包含、不反向依赖本包）；这里复用同一份，两侧认的是同一个字符串。
from ..normalize import DEFAULT_FUNCTION_NAMESPACE, iter_leaf_tools, model_visible_name


def openai_shaped_tool_call_item_id(item_type: str, tool_id: str) -> str:
    """给工具调用 item 补上 OpenAI 形状的 id 前缀。"""
    prefix = _ITEM_ID_PREFIX.get(item_type)
    if prefix is None or not tool_id or tool_id.startswith(prefix):
        return tool_id
    return f"{prefix}_{tool_id}"


def extract_custom_tool_names(tools: Sequence[object] | None) -> set[str]:
    """挑出所有原本声明为 ``type: "custom"`` 的工具名（模型侧可见名）。

    这些东西在响应侧要还原成 ``custom_tool_call`` item —— 请求侧把它们当 function
    工具发出去，回来的是 function_call，只有靠这份名字集合才能认出来。
    namespace 容器里的 custom 工具也在内，名字同样按模型侧规则算（和请求侧发给
    上游的 function 名保持一致，两边认的是同一个字符串）。
    """
    if not tools:
        return set()
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") == "custom" and tool.get("name"):
            names.add(str(tool["name"]))
        elif tool.get("type") == "namespace":
            namespace = str(tool.get("name") or "")
            inner = tool.get("tools")
            for sub in inner if isinstance(inner, list) else ():
                if isinstance(sub, Mapping) and sub.get("type") == "custom" and sub.get("name"):
                    names.add(model_visible_name(namespace, str(sub["name"])))
    return names


def serialize_tool_call_arguments(raw_arguments: object, default: str = "") -> str:
    """把工具参数渲染成 JSON 字符串。

    正常来的已经是 JSON 串，但客户端和上游也都会直接给解码后的对象。对 dict 直接
    ``str()`` 出来的是单引号 Python repr，任何 JSON 解析器都会报
    ``Expecting ',' delimiter``。
    """
    if isinstance(raw_arguments, str):
        return raw_arguments or default
    if raw_arguments is None:
        return default
    return json.dumps(raw_arguments, default=str, ensure_ascii=False)


def unwrap_custom_tool_arguments(arguments: str) -> str:
    """从 ``{"content": "..."}`` 里取出裸 content 字符串。

    custom 工具转成 function 工具后 schema 是 ``{"properties":{"content":...}}``，
    模型回的参数形如 ``{"content": "*** Begin Patch\\n..."}``；而 Responses 的
    ``custom_tool_call.input`` 要的正是里面那个字符串。解不出来就原样返回 ——
    劣质上游有时会直接发裸文本。
    """
    if not arguments:
        return ""
    if len(arguments) > _MAX_ARGUMENTS_LEN:
        return arguments
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError, ValueError):
        return arguments
    if isinstance(parsed, dict) and "content" in parsed:
        return str(parsed["content"])
    return arguments


def is_custom_tool_call(tool_name: str, custom_tool_names: Iterable[str]) -> bool:
    return tool_name in custom_tool_names


def build_tool_call_item_kwargs(
    call_id: str,
    name: str,
    arguments_or_input: str,
    status: str,
    custom_tool_names: Iterable[str],
    namespace: str = "",
    is_custom: bool | None = None,
) -> dict[str, object]:
    """拼一个工具调用 output item（``function_call`` 或 ``custom_tool_call``）。

    custom 工具把 ``arguments`` JSON 解包进 ``input``，普通函数保留原始 ``arguments``
    字符串。流式与非流式共用这一段，两边不会各写一套然后慢慢长歪。

    ``namespace`` 非空且非默认时写进 item（Codex models.rs 的 ``FunctionCall.namespace``
    字段，客户端靠它把调用路由回非默认命名空间的工具）；``is_custom`` 是调用方已经
    判定好的结果（名字拆过前缀之后集合里查不到原名，所以由调用方显式传）。
    """
    custom = is_custom_tool_call(name, custom_tool_names) if is_custom is None else is_custom
    item_type = "custom_tool_call" if custom else "function_call"
    item: dict[str, object] = {
        "type": item_type,
        "id": openai_shaped_tool_call_item_id(item_type, call_id),
        "call_id": call_id,
        "name": name,
        "status": status,
    }
    if namespace and namespace != DEFAULT_FUNCTION_NAMESPACE:
        item["namespace"] = namespace
    if custom:
        # 没跑完的 custom 调用没有 input 可给；空串比缺字段更接近官方形状
        item["input"] = unwrap_custom_tool_arguments(arguments_or_input) if status == "completed" else ""
    else:
        item["arguments"] = arguments_or_input
    return item


# ---------------------------------------------------------------- namespace 名字往返
#
# 请求侧把 namespace 容器展开成 chat function 工具时给名字加了前缀；响应侧模型原样
# 喊回来，得拆回去（裸名 + namespace 字段），Codex 才能把调用路由进正确的命名空间。
# 这张表就是两侧的约定：键是发给上游的模型可见名，值是 (命名空间, 裸名)。
# 参考 litellm ``namespace_tool_name_map``（transformation.py :2003）。


def namespace_name_map(all_tools: Sequence[object] | None) -> dict[str, tuple[str, str]]:
    """建「模型可见名 -> (命名空间, 裸名)」的往返表。

    只收**非默认**命名空间 —— 默认（``functions``）的工具本来就是裸名往返，不需要查表。
    裸名无歧义（不在顶层、命名空间里也只出现一次）时同时登记裸名条目：模型有时不守
    约定喊裸名，能接住就接住。歧义裸名不猜 —— 两个命名空间都有 ``open`` 时，瞎猜
    会把调用路由到错的工具上。
    """
    entries: list[tuple[str, str]] = []
    top_level_names: set[str] = set()
    for tool in all_tools or ():
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") == "function" and tool.get("name"):
            top_level_names.add(str(tool["name"]))
        elif tool.get("type") == "namespace":
            namespace = str(tool.get("name") or "")
            if not namespace or namespace == DEFAULT_FUNCTION_NAMESPACE:
                continue
            inner = tool.get("tools")
            for sub in inner if isinstance(inner, list) else ():
                if isinstance(sub, Mapping) and sub.get("type") in ("function", "custom") and sub.get("name"):
                    entries.append((namespace, str(sub["name"])))

    visible: dict[str, tuple[str, str]] = {}
    bare_counts: dict[str, int] = {}
    for namespace, bare in entries:
        visible[model_visible_name(namespace, bare)] = (namespace, bare)
        bare_counts[bare] = bare_counts.get(bare, 0) + 1
    for namespace, bare in entries:
        if bare_counts[bare] == 1 and bare not in top_level_names:
            visible.setdefault(bare, (namespace, bare))
    return visible


def qualify_tool_name(name: str, namespace: object, ns_map: Mapping[str, tuple[str, str]]) -> str:
    """请求侧：历史里一条 function_call 的名字 -> chat 侧该用的名字。

    item 自带 ``namespace`` 字段时以它为准（客户端明确的不会被表覆盖）；没有字段就
    查表（限定名或无歧义裸名都认），查不到原样放行。
    """
    if isinstance(namespace, str) and namespace:
        return model_visible_name(namespace, name)
    mapped = ns_map.get(name)
    if mapped is not None:
        return model_visible_name(mapped[0], mapped[1])
    return name


def split_tool_name(name: str, ns_map: Mapping[str, tuple[str, str]]) -> tuple[str, str]:
    """响应侧：模型喊的名字 -> (裸名, 命名空间)。查不到就 (原名, "")。"""
    mapped = ns_map.get(name)
    if mapped is not None:
        return mapped[1], mapped[0]
    return name, ""


def validated_allowed_callers(value: object) -> list[str] | None:
    """``allowed_callers`` 只接受字符串列表；给了别的形状就报错而不是悄悄丢掉。"""
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise BridgeError("allowed_callers 必须是字符串数组")


def _grammar_suffix(fmt: object) -> str:
    """把 custom 工具的 grammar 拼成 markdown 代码块塞进 description。

    chat 上游没有 grammar 约束这个概念，唯一能让模型照格式产出的办法就是把它写进
    描述里。
    """
    if not isinstance(fmt, Mapping):
        return ""
    definition = fmt.get("definition")
    if not isinstance(definition, str) or not definition:
        return ""
    syntax = fmt.get("syntax") if isinstance(fmt.get("syntax"), str) else ""
    return f"\n\nFormat:\n```{syntax}\n{definition}\n```"


def convert_custom_tool_to_function_tool(tool: Mapping[str, object]) -> dict[str, object] | None:
    """把 Responses 的 ``type: "custom"`` 工具转成 Chat Completions 的 function 工具。

    参数固定成一个 ``content`` 字符串 —— 自由格式工具在 chat 那边的唯一表达方式。
    不是 custom 工具就返回 None。
    """
    if tool.get("type") != "custom":
        return None
    name = tool.get("name") if isinstance(tool.get("name"), str) else ""
    raw_description = tool.get("description")
    description = (raw_description if isinstance(raw_description, str) else "") + _grammar_suffix(tool.get("format"))
    function: dict[str, object] = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": _CONTENT_PARAM_DESCRIPTION.format(name=name),
                }
            },
            "required": ["content"],
        },
    }
    chat_tool: dict[str, object] = {"type": "function", "function": function}
    allowed_callers = validated_allowed_callers(tool.get("allowed_callers"))
    if allowed_callers is not None:
        chat_tool["allowed_callers"] = allowed_callers
    return chat_tool


def _function_tool(tool: Mapping[str, object]) -> dict[str, object]:
    """原样透传一个 function 工具，只把 parameters 补上 type。

    Responses 允许省略 ``parameters.type``，chat 上游普遍要它。
    """
    raw_parameters = tool.get("parameters")
    parameters = dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {}
    if "type" not in parameters:
        parameters["type"] = "object"
    function: dict[str, object] = {
        "name": tool.get("name") or "",
        "description": tool.get("description") or "",
        "parameters": parameters,
        "strict": bool(tool.get("strict", False)),
    }
    chat_tool: dict[str, object] = {"type": "function", "function": function}
    # 这几个是 Responses 侧的调优字段，litellm 也透传，留着比丢掉好（不认的站会忽略）
    for key in ("cache_control", "defer_loading", "allowed_callers", "input_examples"):
        if tool.get(key) is not None:
            chat_tool[key] = tool[key]
    return chat_tool


def _renamed_function_tool(tool: Mapping[str, object], namespace: str) -> dict[str, object]:
    """function 工具 -> chat function 工具，名字带命名空间前缀（见 tools_for_chat）。"""
    return _function_tool({**tool, "name": model_visible_name(namespace, str(tool.get("name") or ""))})


def tools_for_chat(tools: Sequence[object] | None) -> tuple[list[dict], list[str]]:
    """Responses 的 tools 数组 -> chat 的 tools 数组，外加被丢掉的类型名。

    namespace 容器在这里展开：默认容器（``functions``）里的工具用裸名，其它容器
    按 Codex 的模型侧命名规则加前缀（``model_visible_name``）。

    被丢掉的都是「chat 上游表达不了」的：``web_search`` / ``web_search_preview``
    是服务端搜索（方案决策 5），``local_shell`` / ``computer_use`` /
    ``image_generation`` / ``mcp`` 之类要么是服务端执行、要么是另一种线形状。
    硬透传下去，多数站会直接 400 掉整个请求 —— 丢一个工具比丢整轮对话好。

    **不静默**：丢掉的类型会记进转发记录的 note，日志里能看出来这轮少了什么。
    """
    chat_tools: list[dict] = []
    dropped: list[str] = []
    for namespace, tool in iter_leaf_tools(tools):
        kind = tool.get("type")
        if kind == "function":
            chat_tools.append(_function_tool(tool) if not namespace else _renamed_function_tool(tool, namespace))
        elif kind == "custom":
            named = {**tool, "name": model_visible_name(namespace, str(tool.get("name") or ""))}
            converted = convert_custom_tool_to_function_tool(named)
            if converted is not None:
                chat_tools.append(converted)
        elif kind in _CONVERTIBLE_TOOL_TYPES:
            continue
        else:
            dropped.append(str(kind or "?"))
    return chat_tools, dropped


def tool_choice_for_chat(tool_choice: object) -> str | dict | None:
    """Responses 的 tool_choice -> chat 的 tool_choice（litellm ``_transform_tool_choice``）。

    Cursor 那类客户端会发 ``{"type": "tool"}`` 这种 chat 不认的形状；
    ``{"type": "custom", "name": ...}`` 也要落到 ``{"type":"function",...}`` 上。
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, Mapping):
        return None
    function = tool_choice.get("function")
    if isinstance(function, Mapping) and function.get("name"):
        return dict(tool_choice)  # 已经是 chat 形状
    kind = tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "none":
        return "none"
    if kind in ("required", "tool", "any"):
        return "required"
    if kind == "function":
        name = tool_choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    if kind == "custom":
        custom = tool_choice.get("custom")
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name:
            name = custom.get("name") if isinstance(custom, Mapping) else None
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
        return "required"
    return dict(tool_choice)


def tool_choice_for_response(tool_choice: object) -> str | dict:
    """把客户端发来的 tool_choice 原样回填进 Responses 响应（litellm :275）。

    客户端看到的是自己发出去的那个形状，不是上游 chat 那边的归一化结果。
    """
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, Mapping):
        return "auto"
    kind = tool_choice.get("type")
    if kind == "custom":
        custom = tool_choice.get("custom")
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name:
            name = custom.get("name") if isinstance(custom, Mapping) else None
        return {"type": "custom", "name": name} if isinstance(name, str) and name else "auto"
    function = tool_choice.get("function")
    if isinstance(function, Mapping) and function.get("name"):
        return {"type": "function", "name": function["name"]}
    name = tool_choice.get("name")
    if kind == "function" and isinstance(name, str) and name:
        return {"type": "function", "name": name}
    if kind in ("auto", "none", "required"):
        return kind
    return "auto"


# 上游可能把 ``call_id`` 填成 ``call_0`` / ``call_1`` 这种按 index 复位的假 id，
# 同一个会话里第二轮就撞车。撞上这种形状时改用唯一 item id（litellm :2160）。
_INDEX_LIKE_CALL_ID: Final = re.compile(r"call_\d+")


def tool_call_correlation_id(item_id: str | None, call_id: str | None) -> str:
    """挑一个能跨轮次唯一标识这次工具调用的 id。"""
    if call_id and _INDEX_LIKE_CALL_ID.fullmatch(call_id) is None:
        return call_id
    return item_id or call_id or ""


# ---------------------------------------------------------------- 工具调用备忘
#
# 为什么需要它：客户端把历史原样发回来时，``function_call_output`` 的 call_id 一定
# 配得上前面某条 ``function_call``，不需要任何缓存。但 Codex 压缩过历史之后，中间的
# function_call 可能整段消失，只剩一条孤零零的 tool 结果 —— 发给 chat 上游就是一条
# 没有对应 tool_call 的 ``role: tool``，DeepSeek 这类站会直接 400。
#
# litellm 为此维护了一个全局 TOOL_CALLS_CACHE（响应侧写入、请求侧读取）。这里做同一
# 件事，只是限个容量：网关是长驻进程，不设上限就是一个慢性泄漏。
_TOOL_CALL_MEMORY: Final[dict[str, tuple[str, str]]] = {}
_TOOL_CALL_MEMORY_MAX: Final = 512


def remember_tool_call(call_id: str, name: str, arguments: str) -> None:
    """记下这次工具调用的名字和参数，供后续轮次修复孤立的 tool 结果。"""
    if not call_id:
        return
    if len(_TOOL_CALL_MEMORY) >= _TOOL_CALL_MEMORY_MAX:
        # 直接丢掉最早插入的那条：OrderedDict 语义靠 dict 的插入序，够用
        _TOOL_CALL_MEMORY.pop(next(iter(_TOOL_CALL_MEMORY)), None)
    _TOOL_CALL_MEMORY[call_id] = (name, arguments)


def recall_tool_call(call_id: str) -> tuple[str, str] | None:
    return _TOOL_CALL_MEMORY.get(call_id)


def forget_all_tool_calls() -> None:
    """清空备忘。只有测试需要 —— 它是进程内的，用例之间会互相串。"""
    _TOOL_CALL_MEMORY.clear()
