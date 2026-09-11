"""协议桥接 · 流式：chat 的 SSE chunk -> Responses 的事件流。

用例改造自 litellm v1.102.0 的黄金用例
``tests/test_litellm/responses/litellm_completion_transformation/test_streaming_iterator_transformation.py``：
那边断言 pydantic 事件对象，这边断言「喂 chat chunk 列表 -> 事件列表（类型、顺序、
关键字段、sequence_number）」。
"""

from __future__ import annotations

import json

from helpers import bridge_events, chat_chunk, chat_tool_delta, chat_usage, codex_request, sse_chunks

from gateway.bridge import StreamBridge, all_tools_of


def run(raw: bytes, *, request: dict | None = None, chunk_size: int | None = None) -> list[dict]:
    """跑一遍桥接，返回事件列表。``chunk_size`` 用来把上游字节切碎，验证帧缓冲。"""
    request = request if request is not None else codex_request()
    bridge = StreamBridge(request, "gpt-5.6-luna", all_tools=all_tools_of(request))
    out: list[bytes] = []
    if chunk_size:
        for start in range(0, len(raw), chunk_size):
            out += bridge.feed(raw[start : start + chunk_size])
    else:
        out += bridge.feed(raw)
    out += bridge.finish()
    return bridge_events(b"".join(out))


def types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def test_text_only_stream_produces_the_full_event_sequence():
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": ""}),
        chat_chunk({"content": "你好"}),
        chat_chunk({"content": "，世界"}, finish_reason="stop", usage=chat_usage()),
    ))

    assert types(events) == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [e["delta"] for e in events if e["type"] == "response.output_text.delta"] == ["你好", "，世界"]
    assert next(e for e in events if e["type"] == "response.output_text.done")["text"] == "你好，世界"


def test_sequence_numbers_are_present_monotonic_and_unique():
    """Responses 协议要求 sequence_number 单调；客户端按它排序（流式可能乱序到达）。"""
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": "a"}),
        chat_chunk({"content": "b"}, finish_reason="stop", usage=chat_usage()),
    ))

    numbers = [event["sequence_number"] for event in events]
    assert all(isinstance(n, int) for n in numbers)
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers), "同一个号发两遍会让客户端丢事件"


def test_every_lifecycle_event_carries_the_upstream_response_id():
    """id 必须和上游 chat 的 id 一致：客户端拿它做关联，中途换 id 就认不出来了。"""
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": "hi"}, cid="chatcmpl-abc"),
        chat_chunk({"content": ""}, finish_reason="stop", usage=chat_usage(), cid="chatcmpl-abc"),
    ))

    lifecycle = [event for event in events if "response" in event]
    assert len(lifecycle) == 3, "created / in_progress / completed 才带 response 对象"
    for event in lifecycle:
        assert event["response"]["id"] == "chatcmpl-abc"


def test_frames_split_across_network_chunks_are_buffered():
    """网络 chunk 只是传输层分片。完成事件经常被切成两半，按 chunk 解析会丢事件。"""
    raw = sse_chunks(
        chat_chunk({"role": "assistant", "content": "hi"}, cid="chatcmpl-split"),
        chat_chunk({"content": ""}, finish_reason="stop", usage=chat_usage(), cid="chatcmpl-split"),
    )
    events = run(raw, chunk_size=7)

    assert types(events)[-1] == "response.completed"
    assert events[0]["response"]["id"] == "chatcmpl-split"
    assert next(e for e in events if e["type"] == "response.completed")["response"]["usage"]["input_tokens"] == 120


def test_reasoning_first_announces_a_reasoning_item_and_closes_it():
    """第一个有内容的块是推理时，先立 reasoning item，切到正文时再收尾。

    对照 codex-rs 实测行为（2026-09-10 源码确认）：reasoning 和 message **各宣告各的**
    added，正文 delta 只认 ``output_item.added`` 建立的 active item —— 不给正文
    宣告，Codex 会把正文 delta 全部静默丢弃（litellm 的偷懒行为在这里是错的）。
    """
    events = run(sse_chunks(
        chat_chunk({"reasoning_content": "先想"}),
        chat_chunk({"reasoning_content": "再想"}),
        chat_chunk({"content": "答案"}, finish_reason="stop", usage=chat_usage(reasoning=9)),
    ))

    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert [e["item"]["type"] for e in added] == ["reasoning", "message"]
    assert added[0]["item"]["id"].startswith("rs_")
    # codex-rs 把 summary 反序列化成 Vec<_>：null 会让 item 被丢，必须是空数组
    assert added[0]["item"]["summary"] == []
    # reasoning item 的 added 之后、首个 delta 之前要有 part.added（官方流如此）
    types_ = types(events)
    assert types_.index("response.reasoning_summary_part.added") < types_.index("response.reasoning_summary_text.delta")
    # 两个 item 的 output_index 独立递增
    assert [e["output_index"] for e in added] == [0, 1]
    assert types_.count("response.reasoning_summary_text.delta") == 2
    # 推理收尾三件套要排在正文增量之前
    assert types_.index("response.reasoning_summary_text.done") < types_.index("response.output_text.delta")
    assert next(e for e in events if e["type"] == "response.reasoning_summary_text.done")["text"] == "先想再想"
    # 正文 delta 挂在 message item 上（index 1），与快照对齐
    body_delta = next(e for e in events if e["type"] == "response.output_text.delta")
    assert body_delta["item_id"] == added[1]["item"]["id"]
    assert body_delta["output_index"] == 1


def test_reasoning_that_never_gets_followed_by_text_is_still_closed():
    events = run(sse_chunks(
        chat_chunk({"reasoning_content": "只想不答"}),
        chat_chunk({}, finish_reason="stop", usage=chat_usage()),
    ))

    assert "response.reasoning_summary_text.done" in types(events)
    assert "response.reasoning_summary_part.done" in types(events)
    assert types(events)[-1] == "response.completed"


def test_role_only_first_chunk_announces_both_items_in_order():
    """第一块只带 role（content 为空）时，两个 item 都要等各自的首个增量才宣告。

    旧版（litellm 行为）在这里只宣告 message item、推理以「无主 delta」飘着 ——
    codex-rs 源码确认 delta 必须挂在 active item 上，所以推理和正文各宣告各的，
    推理结束照常收尾。
    """
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": ""}),
        chat_chunk({"reasoning_content": "推理"}),
        chat_chunk({"content": "正文"}, finish_reason="stop", usage=chat_usage()),
    ))

    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert [e["item"]["type"] for e in added] == ["reasoning", "message"]

    deltas = [e for e in events if e["type"] == "response.reasoning_summary_text.delta"]
    assert len(deltas) == 1 and deltas[0]["item_id"].startswith("rs_")
    assert deltas[0]["item_id"] != added[1]["item"]["id"]
    assert "response.reasoning_summary_text.done" in types(events)

    completed = next(e for e in events if e["type"] == "response.completed")
    assert [item["type"] for item in completed["response"]["output"]] == ["reasoning", "message"]
    assert completed["response"]["output"][1]["content"][0]["text"] == "正文"


def test_tool_call_arguments_are_chunked_to_match_openai_behaviour():
    """一次性给一大段参数的站（Bedrock 那类）要切成小片，客户端的进度才对得上。"""
    large = '{"param1": "value1", "param2": "value2", "param3": "value3"}'
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_t", name="shell", arguments=large)]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert len(deltas) >= 6
    assert all(len(e["delta"]) <= 10 for e in deltas)
    assert "".join(e["delta"] for e in deltas) == large


def test_tool_call_item_is_announced_at_output_index_zero():
    """output_index 全局递增、宣告顺序 = 分配顺序：纯工具流里工具从 0 起。

    （旧版学 litellm 把 0 留给 message —— 但 codex-rs 根本不读 output_index，
    而全局递增能保证流里 index 和 completed 快照的数组下标处处一致。）
    """
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_t", name="shell", arguments="{}")]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    added = next(e for e in events if e["type"] == "response.output_item.added")
    assert added["output_index"] == 0
    assert added["item"]["type"] == "function_call"
    assert added["item"]["id"] == "fc_call_t"
    assert added["item"]["status"] == "in_progress"

    done = [e for e in events if e["type"] == "response.output_item.done"][0]
    assert done["output_index"] == 0
    assert done["item"]["status"] == "completed"
    assert done["item"]["arguments"] == "{}"

    assert [e["sequence_number"] for e in events] == sorted(e["sequence_number"] for e in events)


def test_tool_arguments_spanning_many_chunks_are_concatenated_by_index():
    """id 只在第一块里出现，后面只有 index + 参数片段 —— 靠 index 映射接起来。"""
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_a", name="shell", arguments='{"lo')]}),
        chat_chunk({"tool_calls": [chat_tool_delta(0, arguments='cation":')]}),
        chat_chunk({"tool_calls": [chat_tool_delta(0, arguments=' "SF"}')]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    done = next(e for e in events if e["type"] == "response.function_call_arguments.done")
    assert done["arguments"] == '{"location": "SF"}'
    assert done["item_id"] == "fc_call_a"


def test_reused_index_with_a_different_call_id_is_distrusted():
    """有的站复用 index：这时绝不能把新调用的参数拼到旧调用上。"""
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_a", name="shell", arguments="{}")]}),
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_b", name="shell", arguments="{}")]}),
        chat_chunk({"tool_calls": [chat_tool_delta(0, arguments='{"dropped":true}')]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    done = {e["item_id"]: e["arguments"] for e in events if e["type"] == "response.function_call_arguments.done"}
    assert done == {"fc_call_a": "{}", "fc_call_b": "{}"}


def test_two_tool_calls_get_consecutive_output_indexes():
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="c1", name="shell", arguments="{}")]}),
        chat_chunk({"tool_calls": [chat_tool_delta(1, call_id="c2", name="shell", arguments="{}")]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    added = [e for e in events if e["type"] == "response.output_item.added"]
    assert [e["output_index"] for e in added] == [0, 1]


def test_custom_tool_call_is_unwrapped_in_the_completed_snapshot():
    """apply_patch 在流式里也要还原成 custom_tool_call，input 是裸 patch 文本。"""
    patch = "*** Begin Patch\n*** End Patch\n"
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(
            0, call_id="call_p", name="apply_patch",
            arguments=json.dumps({"content": patch}, ensure_ascii=False))]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    added = next(e for e in events if e["type"] == "response.output_item.added")
    assert added["item"]["type"] == "custom_tool_call"
    assert added["item"]["id"] == "ctc_call_p"

    completed = next(e for e in events if e["type"] == "response.completed")
    item = completed["response"]["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["input"] == patch


def test_tool_only_stream_still_closes_the_message_item():
    """litellm 无条件发正文的收尾三件套，哪怕这条流一个正文字都没出。照抄。"""
    events = run(sse_chunks(
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="c", name="shell", arguments="{}")]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    assert types(events)[-4:] == [
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert next(e for e in events if e["type"] == "response.completed")["response"]["output"][0]["type"] == "function_call"


def test_completed_snapshot_reuses_the_item_ids_seen_mid_stream():
    """客户端拿 mid-stream 看到的 id 拼下一轮请求，快照里换 id 它就找不着了。"""
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": "hi"}),
        chat_chunk({"tool_calls": [chat_tool_delta(0, call_id="call_t", name="shell", arguments="{}")]}),
        chat_chunk({}, finish_reason="tool_calls", usage=chat_usage()),
    ))

    streamed_message_id = next(e for e in events if e["type"] == "response.output_item.added")["item"]["id"]
    streamed_tool_id = next(
        e for e in events if e["type"] == "response.output_item.added" and e["item"]["type"] == "function_call"
    )["item"]["id"]

    completed = next(e for e in events if e["type"] == "response.completed")["response"]
    assert completed["output"][0]["id"] == streamed_message_id
    assert completed["output"][1]["id"] == streamed_tool_id


def test_completed_event_carries_usage():
    """没有 usage，Codex 不认这个 response.completed。"""
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": "hi"}),
        chat_chunk({"content": ""}, finish_reason="stop", usage=chat_usage(prompt=999, completion=111, cached=900)),
    ))

    usage = next(e for e in events if e["type"] == "response.completed")["response"]["usage"]
    assert usage == {
        "input_tokens": 999,
        "input_tokens_details": {"cached_tokens": 900},
        "output_tokens": 111,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 1110,
    }


def test_stream_ends_with_the_done_marker_like_litellm():
    """Responses 协议本身没有 [DONE]，但 litellm 的 proxy 会补一发，客户端两种都认。"""
    request = codex_request()
    bridge = StreamBridge(request, "m", all_tools=all_tools_of(request))
    out: list[bytes] = []
    out += bridge.feed(sse_chunks(chat_chunk({"role": "assistant", "content": "x"})))
    out += bridge.finish()

    assert b"".join(out).rstrip().endswith(b"data: [DONE]")


def test_final_frame_without_a_blank_line_is_still_parsed():
    """劣质站最后一帧不带收尾空行，也要认。"""
    events = run(
        sse_chunks(chat_chunk({"role": "assistant", "content": "hi"}), done=False)
        + b'data: {"id":"chatcmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
          b'"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}'
    )

    assert types(events)[-1] == "response.completed"
    assert events[-1]["response"]["usage"]["input_tokens"] == 5


def test_mid_stream_error_emits_response_failed_and_no_completed():
    """上游在流里回错误：不能只记日志，客户端会一直等收尾事件。"""
    request = codex_request()
    bridge = StreamBridge(request, "m", all_tools=all_tools_of(request))
    raw = b"".join([
        sse_chunks(chat_chunk({"role": "assistant", "content": "开始"}), done=False),
        b'data: {"error": {"message": "upstream died", "code": 500}}\n\n',
    ])
    out = bridge.feed(raw) + bridge.finish()
    events = bridge_events(b"".join(out))

    assert bridge.failed is True
    assert types(events)[-1] == "response.failed"
    assert "response.completed" not in types(events)
    assert "upstream died" in json.dumps(events[-1], ensure_ascii=False)


def test_upstream_that_dies_before_any_chunk_still_gets_a_created_event():
    """上游一个 chunk 都没给就断了：至少要给出 created，客户端才知道流开始了。"""
    request = codex_request()
    bridge = StreamBridge(request, "m", all_tools=all_tools_of(request))
    events = bridge_events(b"".join(bridge.finish()))

    assert types(events)[0] == "response.created"
    assert types(events)[-1] == "response.completed"


def test_response_model_follows_the_upstream_like_the_non_streaming_path():
    """``model`` 沿用上游回的真名（方案 §3.2），和客户端请求里的名字无关。

    OpenAI 自己的行为就是这样（请求 gpt-5 会回 gpt-5-2025-xx 那个快照名），
    客户端不会因此错乱；而把候选的上游真名藏起来反而让排查少一条线索。
    """
    events = run(sse_chunks(
        chat_chunk({"role": "assistant", "content": "hi"}, model="deepseek-v4-pro"),
        chat_chunk({"content": ""}, finish_reason="stop", usage=chat_usage(), model="deepseek-v4-pro"),
    ))

    assert events[0]["response"]["model"] == "deepseek-v4-pro"
    assert events[0]["type"] == "response.created"
    assert events[0]["response"]["status"] == "in_progress"


def test_created_event_echoes_tools_including_hoisted_ones():
    events = run(sse_chunks(chat_chunk({"role": "assistant", "content": "hi"}, finish_reason="stop")))

    tools = events[0]["response"]["tools"]
    # 回显的是**原始** tools 数组 —— namespace 容器不拆开，客户端要看到自己发的东西
    assert [tool.get("name") or tool.get("type") for tool in tools] == ["functions", "web_search"]
