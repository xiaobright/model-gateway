"""流式映射：Chat Completions 的 SSE 帧 -> Responses 的 SSE 事件流。

移植自 litellm v1.102.0（MIT），原文件
``litellm/responses/litellm_completion_transformation/streaming_iterator.py``：

- ``LiteLLMCompletionStreamingIterator``（:73）
- ``_ensure_output_item_for_chunk``（:825，output item 的懒创建）
- ``_transform_chat_completion_chunk_to_response_api_chunk``（:1057）
- ``_queue_tool_call_delta_events``（:175）/ ``_queue_final_tool_call_done_events``（:261）
- ``return_default_initial_events``（:773）/ ``return_default_done_events``（:759）
- ``common_done_event_logic``（:793）/ ``_emit_response_completed_event``（:1173）

和 litellm 的三处**有意差异**，都是为了让「网关」这个位置更稳：

1. litellm 靠 ``stream_chunk_builder`` 把整个流重建成 pydantic 的 ModelResponse
   再转换。我们不引入那一套，边收边攒（文本、推理、工具参数、usage）。代价是拿不到
   「只在最终对象里出现、从没作为 delta 来过」的工具调用 —— 真实 chat 站都会把它们
   当 delta 发，这条不算丢东西。
2. 每个事件都带自增的 ``sequence_number``（含 ``response.completed``）。litellm 有几处
   忘了赋值、有一个硬编码成 1；Responses 协议要求它单调，这里统一给对。
3. 上游中途回 ``{"error": ...}`` 时补发 ``response.failed``。litellm 那条路会抛异常，
   客户端一直等不到收尾事件。
4. **item 宣告策略重写**（2026-09-10，依据 codex-rs 源码 + 官方抓包 + moon-bridge 实证）：
   litellm 每条流只发一次 ``output_item.added``（推理先行就不给正文发了），这在 Codex
   上是灾难 —— codex-rs 的正文/思考 delta 都要求存在 active item，而 active item 只能
   由 ``output_item.added`` 建立；且 ``ReasoningItem.summary`` 是 ``Vec<_>``，litellm 式
   的 ``null`` 会让 reasoning item 反序列化失败被丢。这里改成：reasoning / message /
   工具各宣告各的，``summary`` 恒为 ``[]``，首个 ``summary delta`` 前补
   ``reasoning_summary_part.added``，``output_index`` 全局递增（= completed 快照里的
   数组下标）。

线格式按 litellm proxy 的实际输出：``data: {json}\\n\\n``，**没有 event: 行**
（事件名在 JSON 的 ``type`` 里），末尾补一发 ``data: [DONE]``。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from . import request as request_mod
from . import response as response_mod
from . import tools as tools_mod

# SSE 帧之间用空行分隔。上游可能用 \n 或 \r\n，两种都得认
_FRAME_SPLIT: Final = re.compile(rb"\r?\n\r?\n")

# 工具参数按 10 字符切片下发，模仿 OpenAI token 级别的流式行为。
# Bedrock 那类站会把整段参数一次发完，客户端按「大块 = 上限」估算进度会失真。
_ARGUMENT_CHUNK: Final = 10


def _frame_data(frame: bytes) -> str:
    """从一帧 SSE 里取出 data 的内容（多行 data 用 \\n 接起来）。"""
    lines: list[bytes] = []
    for line in frame.splitlines():
        if line.startswith(b":"):
            continue
        if line.startswith(b"data:"):
            lines.append(line[5:].lstrip())
    return b"\n".join(lines).decode("utf-8", "ignore").strip()


class StreamBridge:
    """把一个 chat 的 SSE 字节流实时翻成 Responses 的事件流。

    ``feed`` 吃原始字节、吐要发给客户端的字节；``finish`` 收尾。内部维护未完整帧的
    buffer —— 网络 chunk 只是传输层分片，不能拿它当帧边界（完成事件常常被切成两半）。
    """

    def __init__(
        self,
        request: Mapping[str, object],
        model: str,
        *,
        all_tools: Sequence[object] | None = None,
    ) -> None:
        self._request = request
        self._model = model
        self._custom_names = tools_mod.extract_custom_tool_names(all_tools)
        self._ns_map = tools_mod.namespace_name_map(all_tools)
        self._buffer = bytearray()
        self._pending: list[dict] = []
        self._seq = 0

        self._started = False
        self._finished = False
        self._done_marker = False
        self._failed = False

        self._response_id: str | None = None
        self._created: int | None = None
        self._upstream_model: str | None = None
        self._finish_reason: str | None = None
        self._usage: dict | None = None

        self._message_item_id: str | None = None
        self._reasoning_item_id: str | None = None
        self._reasoning_announced = False
        self._message_announced = False

        # output_index 全局递增，宣告顺序 = 分配顺序。这样流里事件的 index 和
        # response.completed 快照里 output 数组的下标在所有组合下都一致。
        # （对照官方抓包 + codex-rs：每个 item 独立 index；codex 虽然不读这个
        # 字段，但严格按协议来的客户端会读。）
        self._next_output_index = 0
        self._reasoning_output_index: int | None = None
        self._message_output_index: int | None = None

        self._text_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._reasoning_active = False
        self._reasoning_done = False

        self._tool_output_index: dict[str, int] = {}
        self._tool_args: dict[str, str] = {}
        self._tool_item_id: dict[str, str] = {}
        self._tool_name: dict[str, str] = {}
        self._tool_call_id_by_index: dict[int, str] = {}
        self._ambiguous_indexes: set[int] = set()

    # ------------------------------------------------------------ 对外接口

    def feed(self, chunk: bytes) -> list[bytes]:
        """喂一块上游字节，返回这次要发给客户端的事件字节。"""
        if self._finished:
            return []
        self._buffer.extend(chunk)
        while True:
            match = _FRAME_SPLIT.search(self._buffer)
            if match is None:
                return self._drain()
            frame = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            self._on_frame(frame)

    def finish(self) -> list[bytes]:
        """上游流结束了：把剩下的帧和所有收尾事件发出去。"""
        if self._finished:
            return []
        if self._buffer.strip():
            # 上游最后一帧没有空行收尾（劣质站常见），也得认
            self._on_frame(bytes(self._buffer))
            self._buffer.clear()
        self._emit_final_events()
        self._finished = True
        out = self._drain()
        # Responses 协议本身没有 [DONE]，但 litellm 的 proxy 会补一发，客户端
        # （含网关自己的 SSEObserver）两种都认。补上，兼容性更宽
        out.append(b"data: [DONE]\n\n")
        return out

    @property
    def failed(self) -> bool:
        """上游在流里回了 error，已经给客户端发过 response.failed。"""
        return self._failed

    @property
    def completed(self) -> bool:
        """收到过 response.completed。"""
        return self._finished and not self._failed

    # ------------------------------------------------------------ 帧处理

    def _on_frame(self, frame: bytes) -> None:
        text = _frame_data(frame)
        if not text:
            return
        if text == "[DONE]":
            self._done_marker = True
            return
        try:
            chunk = json.loads(text)
        except ValueError:
            return
        if not isinstance(chunk, Mapping):
            return

        if self._error_of(chunk) is not None:
            self._emit_failed(self._error_of(chunk))
            return

        self._adopt(chunk)
        if not self._started:
            self._start()
        self._on_chunk(chunk)

    @staticmethod
    def _error_of(chunk: Mapping[str, object]) -> object | None:
        """挑出流里的错误帧。有 choices 的帧永远不算错误。"""
        error = chunk.get("error")
        if error is None:
            return None
        if isinstance(chunk.get("choices"), list) and chunk["choices"]:
            return None
        return error

    def _adopt(self, chunk: Mapping[str, object]) -> None:
        """从 chunk 里取响应 id / 创建时间 / 模型名 / usage。

        id 要在 ``response.created`` 之前拿到 —— 所有事件带的都是同一个响应 id，
        客户端拿它做关联，中途换 id 会让它认不出来。
        """
        if self._response_id is None:
            chunk_id = chunk.get("id")
            if isinstance(chunk_id, str) and chunk_id:
                self._response_id = chunk_id
        if self._created is None:
            created = chunk.get("created")
            if isinstance(created, int):
                self._created = created
        if self._upstream_model is None:
            model = chunk.get("model")
            if isinstance(model, str) and model:
                self._upstream_model = model
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self._usage = dict(usage)

    def _start(self) -> None:
        """发 ``response.created`` / ``response.in_progress``。

        故意等到第一个 chunk 到齐才发：上游的真实 id 在第一个 chunk 里，先发一个
        自己编的 id 再改口，客户端会认成两个响应。
        """
        self._started = True
        self._pending.append(self._event("response.created", self._response_snapshot("in_progress")))
        self._pending.append(self._event("response.in_progress", self._response_snapshot("in_progress")))

    # ------------------------------------------------------------ chunk 处理

    def _on_chunk(self, chunk: Mapping[str, object]) -> None:
        choices = chunk.get("choices")
        choice = None
        if isinstance(choices, Sequence) and not isinstance(choices, (str, bytes)) and choices:
            if isinstance(choices[0], Mapping):
                choice = choices[0]
        delta: Mapping[str, object] = {}
        if choice is not None and isinstance(choice.get("delta"), Mapping):
            delta = choice["delta"]
        finish_reason = choice.get("finish_reason") if choice is not None else None
        if isinstance(finish_reason, str) and finish_reason:
            self._finish_reason = finish_reason

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self._ensure_reasoning_item()
            self._reasoning_parts.append(reasoning)
            self._seq += 1
            self._pending.append(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": self._reasoning_item_id,
                    "output_index": self._reasoning_output_index,
                    "summary_index": 0,
                    "delta": reasoning,
                    "sequence_number": self._seq,
                }
            )
            return

        if self._reasoning_active and not self._reasoning_done:
            # 这一块已经不是推理了，说明思考结束 —— 收尾事件要在本块自己的事件之前发
            if delta.get("content") or delta.get("tool_calls") or finish_reason is not None:
                self._close_reasoning()

        content = delta.get("content")
        if isinstance(content, str) and content:
            self._ensure_message_item()
            self._text_parts.append(content)
            self._seq += 1
            self._pending.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": self._message_item_id,
                    "output_index": self._message_output_index,
                    "content_index": 0,
                    "delta": content,
                    "sequence_number": self._seq,
                }
            )
            return

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self._queue_tool_deltas(tool_calls)
            return

    # ------------------------------------------------------------ item 宣告

    def _alloc_output_index(self) -> int:
        index = self._next_output_index
        self._next_output_index += 1
        return index

    def _ensure_reasoning_item(self) -> None:
        """第一条推理增量到来时宣告 reasoning item。

        item 的 ``summary`` 必须是 **空数组** 而不是 null：Codex（codex-rs）把
        ``ReasoningItem.summary`` 反序列化成 ``Vec<_>``（无 default、非 Option），
        发 null 会让整个 item 解析失败被静默丢弃 —— 思考块建不起来，后续的
        推理 delta 在它那边全部「无主」丢掉，折叠/流式思考就都没了。

        ``reasoning_summary_part.added`` 也要先发（官方流如此）：Codex 把它当
        「思考分节」事件，moon-bridge 的实证是带上最稳。
        """
        if self._reasoning_announced:
            return
        self._reasoning_announced = True
        self._reasoning_active = True
        self._reasoning_output_index = self._alloc_output_index()
        if self._reasoning_item_id is None:
            self._reasoning_item_id = f"rs_{uuid.uuid4()}"
        self._seq += 1
        self._pending.append(
            {
                "type": "response.output_item.added",
                "output_index": self._reasoning_output_index,
                "sequence_number": self._seq,
                "item": {
                    "id": self._reasoning_item_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": self._reasoning_item_id,
                "output_index": self._reasoning_output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
                "sequence_number": self._seq,
            }
        )

    def _ensure_message_item(self) -> None:
        """第一条正文增量到来时宣告 message item。

        litellm 的偷懒行为（推理先行的流不给正文发 added）在 Codex 上是灾难：
        codex-rs 的 ``OutputTextDelta`` 处理要求存在 active item，而 active item
        **只能**由解析成功的 ``output_item.added`` 建立 —— 不宣告，正文 delta
        全被静默丢弃，正文只在结尾的 ``output_item.done`` 一次性出现，看起来
        就是「没有流式感」。
        """
        if self._message_announced:
            return
        self._message_announced = True
        self._message_output_index = self._alloc_output_index()
        self._ensure_message_id()
        self._seq += 1
        self._pending.append(
            {
                "type": "response.output_item.added",
                "output_index": self._message_output_index,
                "sequence_number": self._seq,
                "item": {
                    "id": self._message_item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.content_part.added",
                "item_id": self._message_item_id,
                "output_index": self._message_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
                "sequence_number": self._seq,
            }
        )

    def _ensure_message_id(self) -> None:
        if self._message_item_id is None:
            self._message_item_id = f"msg_{uuid.uuid4()}"

    def _close_reasoning(self) -> None:
        """推理结束：``text.done`` -> ``part.done`` -> ``output_item.done``。"""
        self._reasoning_done = True
        self._reasoning_active = False
        if self._reasoning_item_id is None:
            self._reasoning_item_id = f"rs_{uuid.uuid4()}"
        if self._reasoning_output_index is None:
            self._reasoning_output_index = self._alloc_output_index()
        text = "".join(self._reasoning_parts)
        item_id = self._reasoning_item_id
        output_index = self._reasoning_output_index

        self._seq += 1
        self._pending.append(
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": 0,
                "text": text,
                "sequence_number": self._seq,
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": item_id,
                "output_index": output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": text},
                "sequence_number": self._seq,
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "sequence_number": self._seq,
                "item": {
                    "id": item_id,
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": text}],
                },
            }
        )

    # ------------------------------------------------------------ 工具调用

    def _tool_index_for(self, call_id: str) -> int:
        existing = self._tool_output_index.get(call_id)
        if existing is not None:
            return existing
        index = self._alloc_output_index()
        self._tool_output_index[call_id] = index
        return index

    def _queue_tool_deltas(self, tool_calls: Sequence[object]) -> None:
        """chat 的 ``tool_calls`` delta -> output_item.added + 参数 delta 事件。

        id 只在第一块里出现，后面几块只带 index。所以要用 index->call_id 的映射兜住；
        **同一个 index 换过 id 就判定这个 index 不可信**（有的站会复用 index），
        宁可跳过也不要张冠李戴地把一段参数拼到另一个调用上。
        """
        for raw in tool_calls:
            if not isinstance(raw, Mapping):
                continue
            index = raw.get("index")
            index = index if isinstance(index, int) else None
            call_id = ""
            raw_id = raw.get("id")
            if isinstance(raw_id, str) and raw_id:
                call_id = raw_id
                if index is not None:
                    existing = self._tool_call_id_by_index.get(index)
                    if existing is not None and existing != call_id:
                        self._ambiguous_indexes.add(index)
                    self._tool_call_id_by_index[index] = call_id
            elif index is not None:
                if index in self._ambiguous_indexes:
                    continue
                call_id = self._tool_call_id_by_index.get(index, "")
            if not call_id:
                continue

            function = raw.get("function")
            function = function if isinstance(function, Mapping) else {}
            name = str(function.get("name") or "")
            if name:
                self._tool_name[call_id] = name
            arguments_delta = tools_mod.serialize_tool_call_arguments(function.get("arguments"))
            output_index = self._tool_index_for(call_id)

            if call_id not in self._tool_args:
                self._tool_args[call_id] = ""
                self._seq += 1
                item = self._tool_item_kwargs(call_id, "", "in_progress")
                self._tool_item_id[call_id] = str(item["id"])
                self._pending.append(
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "sequence_number": self._seq,
                        "item": item,
                    }
                )

            if arguments_delta:
                self._tool_args[call_id] += arguments_delta
                for start in range(0, len(arguments_delta), _ARGUMENT_CHUNK):
                    self._seq += 1
                    self._pending.append(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": self._tool_item_id.get(call_id, call_id),
                            "output_index": output_index,
                            "delta": arguments_delta[start : start + _ARGUMENT_CHUNK],
                            "sequence_number": self._seq,
                        }
                    )

    def _tool_item_kwargs(self, call_id: str, arguments: str, status: str) -> dict:
        """拼工具调用 item：限定名在这里拆回「裸名 + namespace」（tools.split_tool_name）。

        ``added`` 和 ``done`` 两处必须走同一个方法 —— 同一次调用的两个 item 若一个带
        namespace 一个不带，客户端会对不上。
        """
        name = self._tool_name.get(call_id, "")
        bare, namespace = tools_mod.split_tool_name(name, self._ns_map)
        is_custom = name in self._custom_names or bare in self._custom_names
        return tools_mod.build_tool_call_item_kwargs(
            call_id, bare, arguments, status, self._custom_names,
            namespace=namespace, is_custom=is_custom,
        )

    def _queue_final_tool_events(self) -> None:
        """收尾：每个工具调用补 ``arguments.done`` + ``output_item.done``。

        注意 item 类型按 custom 与否分支 —— 但事件名仍是 ``function_call_arguments.*``，
        这正是 litellm 对 Codex 的做法（它从不发 ``custom_tool_call_input.*``）。
        """
        for call_id, arguments in self._tool_args.items():
            output_index = self._tool_index_for(call_id)
            self._seq += 1
            self._pending.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": self._tool_item_id.get(call_id, call_id),
                    "output_index": output_index,
                    "arguments": arguments,
                    "sequence_number": self._seq,
                }
            )
            self._seq += 1
            item = self._tool_item_kwargs(call_id, arguments, "completed")
            item["id"] = self._tool_item_id.get(call_id, item["id"])
            self._pending.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "sequence_number": self._seq,
                    "item": item,
                }
            )

    # ------------------------------------------------------------ 收尾

    def _emit_final_events(self) -> None:
        if not self._started:
            self._start()
        if self._reasoning_active and not self._reasoning_done:
            self._close_reasoning()
        if self._failed:
            # 已经发过 response.failed 了：再补一个 completed 会让客户端以为这轮成了
            return

        self._queue_final_tool_events()
        self._emit_message_done_events()
        self._pending.append(self._completed_event())

    def _emit_message_done_events(self) -> None:
        """正文的收尾三件套。

        litellm 无条件发这三个（哪怕这条流一个正文字都没出）。纯工具调用的一轮因此
        会看到一组空的 output_text.done —— 客户端不在意，照抄。
        """
        self._ensure_message_id()
        if self._message_output_index is None:
            self._message_output_index = self._alloc_output_index()
        output_index = self._message_output_index
        text = "".join(self._text_parts)
        self._seq += 1
        self._pending.append(
            {
                "type": "response.output_text.done",
                "item_id": self._message_item_id,
                "output_index": output_index,
                "content_index": 0,
                "text": text,
                "sequence_number": self._seq,
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.content_part.done",
                "item_id": self._message_item_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
                "sequence_number": self._seq,
            }
        )
        self._seq += 1
        self._pending.append(
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "sequence_number": self._seq,
                "item": {
                    "id": self._message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                },
            }
        )

    def _assembled_chat(self) -> dict:
        """把攒下来的增量拼成一个 chat 响应，交给非流式映射生成 completed。

        复用同一个函数是有意的：流式和非流式的 output / usage 形状必须一致，
        各写一套迟早会分叉。
        """
        message: dict[str, object] = {"role": "assistant", "content": "".join(self._text_parts) or None}
        reasoning = "".join(self._reasoning_parts)
        if reasoning:
            message["reasoning_content"] = reasoning
        if self._tool_args:
            message["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": self._tool_name.get(call_id, ""),
                        "arguments": arguments,
                    },
                }
                for call_id, arguments in self._tool_args.items()
            ]
        chat: dict[str, object] = {
            "id": self._response_id,
            "created": self._created if self._created is not None else int(time.time()),
            "model": self._upstream_model or self._model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": self._finish_reason or ("tool_calls" if self._tool_args else "stop"),
                    "message": message,
                }
            ],
        }
        if self._usage is not None:
            chat["usage"] = self._usage
        return chat

    def _completed_event(self) -> dict:
        response = response_mod.chat_to_responses(
            self._assembled_chat(),
            request=self._request,
            all_tools=request_mod.all_tools_of(self._request),
        )
        if self._response_id:
            response["id"] = self._response_id
        self._align_output_ids(response)
        self._seq += 1
        return {
            "type": "response.completed",
            "response": response,
            "sequence_number": self._seq,
        }

    def _align_output_ids(self, response: dict) -> None:
        """让 completed 快照里的 item id 和流里已经发过的对得上。

        客户端拿 mid-stream 看到的 id 拼下一轮请求，快照里换了 id 它就找不着了。
        """
        output = response.get("output")
        if not isinstance(output, list):
            return
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message" and self._message_item_id:
                item["id"] = self._message_item_id
            elif kind == "reasoning" and self._reasoning_item_id:
                item["id"] = self._reasoning_item_id
            elif kind in ("function_call", "custom_tool_call"):
                mapped = self._tool_item_id.get(str(item.get("call_id") or ""))
                if mapped:
                    item["id"] = mapped

    def _emit_failed(self, error: object) -> None:
        if self._failed:
            return
        self._failed = True
        if not self._started:
            self._start()
        message = error.get("message") if isinstance(error, Mapping) else str(error)
        response = self._response_snapshot("failed")
        response["error"] = {"code": "upstream_error", "message": message}
        self._seq += 1
        self._pending.append(
            {
                "type": "response.failed",
                "response": response,
                "sequence_number": self._seq,
            }
        )

    def _response_snapshot(self, status: str) -> dict:
        """``response.created`` / ``in_progress`` / ``failed`` 里的 response 骨架。"""
        return {
            "id": self._response_id or f"resp_{uuid.uuid4()}",
            "object": "response",
            "created_at": self._created if self._created is not None else int(time.time()),
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": self._request.get("instructions"),
            "max_output_tokens": self._request.get("max_output_tokens"),
            "model": self._upstream_model or self._model,
            "output": [],
            "parallel_tool_calls": bool(self._request.get("parallel_tool_calls", True)),
            "tool_choice": tools_mod.tool_choice_for_response(self._request.get("tool_choice")),
            "tools": request_mod.all_tools_of(self._request),
            "top_p": self._request.get("top_p") if self._request.get("top_p") is not None else 1.0,
            "usage": None,
        }

    # ------------------------------------------------------------ 编码

    def _event(self, event_type: str, response: dict) -> dict:
        self._seq += 1
        return {"type": event_type, "response": response, "sequence_number": self._seq}

    def _drain(self) -> list[bytes]:
        pending, self._pending = self._pending, []
        return [_encode(event) for event in pending]


def _encode(event: Mapping[str, object]) -> bytes:
    """``data: {json}\\n\\n`` —— 上游/下游都按这个认（事件名在 JSON 的 type 里）。"""
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n".encode()
