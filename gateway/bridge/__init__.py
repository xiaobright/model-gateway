"""协议桥接：Responses 客户端 ↔ Chat Completions 上游。

为什么需要它：Codex 只会说 Responses API，而上游那批站只提供 Chat Completions。
网关本身是字节透传的（客户端打哪个路径就转发到上游同名路径），所以「同一种语言」
是它一直以来的前提；桥接是唯一一处**故意**打破这个前提的地方，也因此被隔离在这
个包里 —— 只有候选显式开了 ``expose_protocol`` 才会走到这里，默认关，没开的行为
和以前一字不差。

对外只有四个名字：``responses_to_chat`` / ``chat_to_responses`` / ``StreamBridge``
/ ``BridgeError``。纯 dict 进、纯 dict 出，不 import pydantic / openai / httpx ——
这样单测不需要起服务，也不需要上游。

实现移植自 litellm v1.102.0（MIT，commit e5da59336d），每个模块头部注明原文件与
函数名。原始出处：https://github.com/BerriAI/litellm
"""

from __future__ import annotations

from .errors import BridgeError
from .request import all_tools_of, responses_to_chat, strip_internal_fields
from .response import chat_to_responses, responses_status
from .stream import StreamBridge

__all__ = [
    "BridgeError",
    "StreamBridge",
    "all_tools_of",
    "chat_to_responses",
    "responses_status",
    "responses_to_chat",
    "strip_internal_fields",
]
