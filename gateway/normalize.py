"""responses-lite → 标准 Responses 的请求规范化（透传层，协议两边都是 Responses）。

Codex Desktop 的 "responses lite" 线格式把工具放在 input 的 ``additional_tools``
item 里、再包一层 namespace 容器 —— OpenAI 的私有扩展。实测（2026-09-10，官方
DeepSeek ``/v1/responses``，直连不走网关）：这种请求 HTTP 200 但模型不认工具，
把调用按训练里的 DSML 标记吐在正文里；同样的工具放顶层 ``tools`` 就返回规范的
``function_call`` / ``custom_tool_call``。第三方「Responses 兼容」上游行为完全一致。

所以这里做**最小**规范化：只把 additional_tools 里的**默认 namespace 容器**
（``functions``）展开合并进顶层 ``tools``（function / custom 类型原样保留，
DeepSeek 对两者都有原生支持）；item 里剩下的（其它 namespace、web_search 之类）
原样留在 input —— 原生支持这些扩展的上游不受影响，不认的上游本来就会忽略。

响应侧零改动：上游返回的就是标准 Responses，继续字节级透传。

本模块自包含（不依赖可选的 bridge 层）：namespace 容器的展开规则也定义在这里，
``gateway.bridge.tools`` 反过来复用。
"""

from __future__ import annotations

from typing import Final, Mapping, Sequence

_ADDITIONAL_TOOLS = "additional_tools"

# Codex 的 "responses lite" 把所有工具包进 namespace 容器（codex-rs
# tools/src/tool_spec.rs ``create_tools_json_for_responses_lite``）：普通函数和
# 自由格式工具全部塞进名为 ``functions`` 的默认容器，其余 namespace 原样另发。
# 容器本身在标准 Responses 上游没有对应物 —— 展开才是唯一正解。
DEFAULT_FUNCTION_NAMESPACE: Final = "functions"


def model_visible_name(namespace: str, name: str) -> str:
    """模型侧看到的调用名（codex-rs tools/src/code_mode.rs :181 的规则）。

    默认命名空间用裸名；其余是 ``{ns}__{name}``，ns 以 ``_`` 结尾（MCP 的
    ``mcp__server`` 就是）或名字以 ``_`` 开头时直接拼接、不再加分隔符。
    响应侧对名字**原样透传**（我们的转换不做任何前缀增删），Codex 自己按这套
    规则解析回去，所以两侧天然一致。
    """
    if not namespace or namespace == DEFAULT_FUNCTION_NAMESPACE:
        return name
    if namespace.endswith("_") or name.startswith("_"):
        return f"{namespace}{name}"
    return f"{namespace}__{name}"


def iter_leaf_tools(tools: Sequence[object] | None) -> list[tuple[str, Mapping[str, object]]]:
    """把 namespace 容器展开成一层，产出 ``(命名空间, 叶子工具)`` 对。

    顶层工具的命名空间是空串；容器里的是容器名。容器的 ``description``
    （"Tools in the X namespace."）拼到叶子工具描述前面 —— 模型看不到容器这一层，
    这段话是它唯一的命名空间语境。
    """
    leaves: list[tuple[str, Mapping[str, object]]] = []
    for tool in tools or ():
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") != "namespace":
            leaves.append(("", tool))
            continue
        namespace = str(tool.get("name") or "")
        ns_description = tool.get("description")
        prefix = f"{ns_description}\n\n" if isinstance(ns_description, str) and ns_description.strip() else ""
        inner = tool.get("tools")
        for sub in inner if isinstance(inner, list) else ():
            if not isinstance(sub, Mapping):
                continue
            if prefix:
                sub = {**sub, "description": prefix + str(sub.get("description") or "")}
            leaves.append((namespace, sub))
    return leaves


def needs_normalization(payload: Mapping[str, object]) -> bool:
    """input 里有没有 additional_tools item（只有 Codex 系客户端会发）。"""
    input_value = payload.get("input")
    items = input_value if isinstance(input_value, list) else ()
    return any(
        isinstance(item, Mapping) and item.get("type") == _ADDITIONAL_TOOLS
        for item in items
    )


def normalize_responses_request(payload: dict) -> tuple[dict, int]:
    """把默认 namespace 容器里的工具提升到顶层 ``tools``。

    返回 ``(规范化后的 payload, 提升的工具数)``；没有可提升的就原样返回（调用方
    继续走「字节级透传」）。非默认 namespace 和其它类型的工具留在原 item 里 ——
    删掉它们会破坏原生扩展上游的语义，留着则不认的上游本来就忽略。
    """
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return payload, 0
    tools = list(payload.get("tools") or [])
    new_input: list[object] = []
    hoisted = 0
    for item in input_value:
        if not (isinstance(item, Mapping) and item.get("type") == _ADDITIONAL_TOOLS):
            new_input.append(item)
            continue
        inner = item.get("tools")
        rest: list[object] = []
        for tool in inner if isinstance(inner, list) else []:
            if isinstance(tool, Mapping) and tool.get("type") == "namespace" and str(
                tool.get("name") or ""
            ) in ("", DEFAULT_FUNCTION_NAMESPACE):
                # 默认容器：叶子展开进顶层 tools。命名空间是默认的，名字不加前缀，
                # 描述也没有容器前缀可拼 —— 和旧线格式的顶层工具一字不差
                for _, leaf in iter_leaf_tools([tool]):
                    tools.append(dict(leaf))
                    hoisted += 1
            else:
                rest.append(tool)
        if rest:
            # 还有非默认内容：留一个瘦身版的 item，别动它原有的其它字段
            new_input.append({**item, "tools": rest})
    if not hoisted:
        return payload, 0
    out = dict(payload)
    out["tools"] = tools
    out["input"] = new_input
    return out, hoisted
