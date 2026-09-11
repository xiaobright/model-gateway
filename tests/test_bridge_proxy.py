"""协议桥接 · 端到端：Responses 客户端打到网关，网关转成 Chat Completions 发上游。

真起网关（测试里是进程内的 TestClient）、真起 mock 上游，走完
`POST /v1/responses` → 转换 → 上游 chat → 转换回来 的整条路。
`tests/test_bridge_*.py` 里那几个文件测的是纯函数，这里测的是「接进网关之后还对」。
"""

from __future__ import annotations

import json

from helpers import (
    MockUpstream,
    add_route,
    add_upstream,
    bridge_events,
    cands,
    chat_usage,
    codex_request,
    wait_rows,
)

PATCH = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** End Patch\n"


def bridge_setup(gateway, mock, *, model="gpt-bridge", remote="deepseek-v4-pro",
                 expose="openai", key=None):
    """在一个 openai-chat 站上建一个「转换后暴露成 Responses」的候选。"""
    group_id = add_upstream(gateway, mock, mock.name, "openai-chat", api_key=key)
    route_id = add_route(gateway, model, group_id, remote, expose_protocol=expose)
    return group_id, route_id


def request_payload(model="gpt-bridge", **extra) -> dict:
    """Codex 的真实请求形状，模型名换成测试用的那个。

    **不带 store** —— 网关把「出现了 store 字段」当成有副作用、不降级，
    降级用例需要它走完整条链。真 Codex 会带 store:false，这是另一回事（见收尾报告）。
    """
    payload = codex_request()
    payload["model"] = model
    payload.pop("store", None)
    payload.update(extra)
    return payload


def test_without_the_bridge_flag_nothing_changes(gateway):
    """默认关：openai-chat 分组的候选不能被 /v1/responses 调到，行为和以前一字不差。"""
    with MockUpstream("siteA") as mock:
        add_upstream(gateway, mock, "siteA", "openai-chat")
        add_route(gateway, "gpt-bridge", _group_of(gateway, "siteA"), "deepseek-v4-pro")

        resp = gateway.post("/v1/responses", json=request_payload())
        assert resp.status_code == 404
        assert "openai-chat" in resp.json()["error"]["message"]
        assert mock.last_chat_request() == {}, "根本没该碰上游"


def test_bridge_rewrites_the_request_and_the_response(gateway):
    """开了开关：请求按 chat 形状发出去，响应按 Responses 形状回来。"""
    with MockUpstream("siteA") as mock:
        mock.script_chat(text="你好", json_mode=True)
        bridge_setup(gateway, mock)

        resp = gateway.post("/v1/responses", json=request_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert [item["type"] for item in body["output"]] == ["message"]
        assert body["output"][0]["content"][0]["text"] == "你好"
        assert body["usage"]["input_tokens"] == 120
        assert body["usage"]["input_tokens_details"]["cached_tokens"] == 80

        sent = mock.last_chat_request()
        assert sent["model"] == "deepseek-v4-pro", "上游只认候选的 remote_model"
        assert sent["messages"][0]["role"] == "system"
        assert sent["messages"][1]["content"] == "把 README 的标题改成中文。"
        names = [tool["function"]["name"] for tool in sent["tools"]]
        assert names == ["shell", "apply_patch"], "工具从 additional_tools 里提上来的"
        assert sent["stream_options"] == {"include_usage": True}
        assert "_bridge_dropped_tools" not in sent, "内部字段不能漏给上游"


def test_bridge_streams_responses_events_with_usage(gateway):
    """流式：上游的 chat chunk 变成 Responses 事件，response.completed 必须带 usage。"""
    with MockUpstream("siteA") as mock:
        mock.script_chat(reasoning="先读代码", text="我来改，",
                         tool_calls=[{"id": "call_9", "name": "apply_patch",
                                      "arguments": {"content": PATCH}}],
                         finish_reason="tool_calls", usage=chat_usage(prompt=999, completion=42))
        bridge_setup(gateway, mock)

        with gateway.stream("POST", "/v1/responses", json=request_payload(stream=True)) as resp:
            assert resp.status_code == 200
            raw = b"".join(resp.iter_bytes())

        events = bridge_events(raw)
        kinds = [event["type"] for event in events]

        assert kinds[0] == "response.created"
        assert kinds[-1] == "response.completed"
        assert raw.rstrip().endswith(b"data: [DONE]")
        # sequence_number 单调且不重复
        numbers = [event["sequence_number"] for event in events]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)

        added = [e for e in events if e["type"] == "response.output_item.added"]
        # reasoning / message / 工具各宣告各的（codex-rs 要求 delta 挂在已宣告的 item 上）
        assert [e["item"]["type"] for e in added] == ["reasoning", "message", "custom_tool_call"]
        assert [e["output_index"] for e in added] == [0, 1, 2]
        assert added[2]["item"]["id"] == "ctc_call_9"

        deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
        assert all(len(e["delta"]) <= 10 for e in deltas)
        assert json.loads("".join(e["delta"] for e in deltas)) == {"content": PATCH}

        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        assert completed["usage"]["input_tokens"] == 999
        custom = next(item for item in completed["output"] if item["type"] == "custom_tool_call")
        assert custom["input"] == PATCH, "custom 工具的 input 要解包成裸 patch 文本"


def test_native_candidate_fails_over_to_the_bridged_one(gateway):
    """混合链（决策 2）：原生 Responses 候选 500 时落到桥接候选，客户端无感。"""
    gateway.post("/admin/api/failover", json={"protocol": "openai", "enabled": True})
    with MockUpstream("native") as native, _mock("chatonly") as chatonly:
        chatonly.script_chat(text="我是替补", json_mode=True)
        native.fail_with(500)
        add_upstream(gateway, native, "native", "openai")
        add_route(gateway, "gpt-bridge", _group_of(gateway, "native"), "gpt-5.6-luna")
        bridge_setup(gateway, chatonly)

        resp = gateway.post("/v1/responses", json=request_payload())
        assert resp.status_code == 200, resp.text
        assert resp.json()["output"][0]["content"][0]["text"] == "我是替补"

        rows = wait_rows(gateway, 2)
        assert [(r["status"], r["attempt"], r["converted"]) for r in rows] == [
            (500, 1, 0),
            (200, 2, 1),
        ]
        assert rows[0]["upstream"] == "native" and rows[0]["note"] == "failed_over"
        assert rows[1]["upstream"] == "chatonly"


def test_bridge_failure_is_a_400_and_does_not_touch_the_breaker(gateway):
    """转换失败是**请求**问题：回 400，不记站点失败，也不该把分组打进冷却。"""
    with MockUpstream("siteA") as mock:
        bridge_setup(gateway, mock)
        payload = request_payload()
        # 认不出来的会话状态 item（决策 6：明确 400 而不是猜形状）
        payload["input"].append({"type": "local_shell_call", "id": "lsh_1", "action": {}})

        resp = gateway.post("/v1/responses", json=payload)
        assert resp.status_code == 400
        assert "local_shell_call" in resp.json()["error"]["message"]
        assert mock.last_chat_request() == {}, "转换失败就不该碰上游"

        rows = wait_rows(gateway, 1)
        assert rows[0]["status"] == 400 and rows[0]["note"] == "bridge_error"
        assert rows[0]["converted"] == 1

        health = gateway.get("/admin/api/failover").json()
        assert [b for b in health["breakers"] if b["group_id"] == _group_of(gateway, "siteA")] == []


def test_previous_response_id_skips_bridged_candidates(gateway):
    """决策 3：有状态请求里桥接候选不参与 —— 客户端拿到的是「没有原生候选」而不是转换错误。"""
    with MockUpstream("siteA") as mock:
        bridge_setup(gateway, mock)
        payload = request_payload(previous_response_id="resp_prev")

        resp = gateway.post("/v1/responses", json=payload)
        assert resp.status_code == 404
        assert "previous_response_id" in resp.json()["error"]["message"]
        assert mock.last_chat_request() == {}


def test_multi_turn_history_round_trips_through_the_bridge(gateway):
    """第二轮：把第一轮返回的 custom_tool_call 原样发回来，网关要能还原成 chat 历史。"""
    with MockUpstream("siteA") as mock:
        mock.script_chat(
            tool_calls=[{"id": "call_1", "name": "apply_patch", "arguments": {"content": PATCH}}],
            finish_reason="tool_calls", json_mode=True,
        )
        bridge_setup(gateway, mock)

        first = gateway.post("/v1/responses", json=request_payload()).json()
        call = next(item for item in first["output"] if item["type"] == "custom_tool_call")

        mock.script_chat(text="改好了", json_mode=True)
        payload = request_payload()
        payload["input"] = list(payload["input"]) + [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "我来改"}]},
            {"type": "custom_tool_call", "id": call["id"], "call_id": call["call_id"],
             "name": call["name"], "input": call["input"], "status": "completed"},
            {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "Success."},
        ]
        second = gateway.post("/v1/responses", json=payload)
        assert second.status_code == 200, second.text

        sent = mock.last_chat_request()
        roles = [message["role"] for message in sent["messages"]]
        assert roles[-2:] == ["assistant", "tool"]
        history_call = sent["messages"][-2]["tool_calls"][0]
        assert history_call["function"]["name"] == "apply_patch"
        assert json.loads(history_call["function"]["arguments"])["content"] == PATCH
        assert sent["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "Success."}


def test_usage_comes_from_the_upstream_chat_frames(gateway):
    """统计口径是上游 chat 字节，不是我们自己生成的事件流（方案 §9 最后一条）。"""
    with MockUpstream("siteA") as mock:
        mock.script_chat(text="hi", json_mode=True, usage=chat_usage(prompt=777, completion=33))
        bridge_setup(gateway, mock)

        gateway.post("/v1/responses", json=request_payload())
        row = wait_rows(gateway, 1)[0]

        assert (row["input_tokens"], row["output_tokens"], row["cached_tokens"]) == (777, 33, 80)
        assert row["protocol"] == "openai", "记录里写的是客户端用的协议"
        assert row["converted"] == 1
        assert row["remote_model"] == "deepseek-v4-pro"


def test_dropped_tools_and_parts_are_written_into_the_log_note(gateway):
    """转换时丢掉的东西必须落在转发记录的备注里。

    丢是故意的（web_search 在 Chat 协议里没有对应物、图片 v1 不做），但**不能无声** ——
    「模型怎么突然不会搜了」这种问题的答案只在这一行里。
    """
    with MockUpstream("siteA") as mock:
        mock.script_chat(text="好", json_mode=True)
        bridge_setup(gateway, mock)

        payload = request_payload()
        payload["input"].append({
            "type": "message", "role": "user",
            "content": [
                {"type": "input_text", "text": "看这张图"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ],
        })
        assert gateway.post("/v1/responses", json=payload).status_code == 200

        note = wait_rows(gateway, 1)[0]["note"]
        assert "tools=web_search" in note, note
        assert "parts=input_image" in note, note
        assert note.startswith("ok"), note


def test_stream_without_usage_still_completes(gateway):
    """上游流式不报 usage（没实现 stream_options 的站）：事件流仍要收尾，别卡住客户端。"""
    with MockUpstream("siteA") as mock:
        mock.script_chat(text="没有 usage", usage=None)
        bridge_setup(gateway, mock)

        with gateway.stream("POST", "/v1/responses", json=request_payload(stream=True)) as resp:
            raw = b"".join(resp.iter_bytes())

        events = bridge_events(raw)
        assert [e["type"] for e in events][-1] == "response.completed"
        usage = events[-1]["response"]["usage"]
        assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0


def test_admin_exposes_and_toggles_the_bridge_flag(gateway):
    """管理端：候选带 bridged 标记，模型按**有效协议**出现在 Responses 的模型清单里。"""
    with MockUpstream("siteA") as mock:
        group_id, route_id = bridge_setup(gateway, mock)

        models = gateway.get("/admin/api/models").json()
        entry = next(m for m in models if m["model_name"] == "gpt-bridge")
        assert entry["protocol"] == "openai"
        candidate = entry["candidates"][0]
        assert (candidate["bridged"], candidate["expose_protocol"]) == (True, "openai")
        assert candidate["group_protocol"] == "openai-chat"

        # 桥接候选也要出现在 Codex 拉的模型清单里，否则它连选都选不到
        listed = [m["id"] for m in gateway.get("/v1/models").json()["data"]]
        assert "gpt-bridge" in listed

        # 关掉：改回原生之后 /v1/responses 就找不到它了
        off = gateway.put("/admin/api/models", json={"route_id": route_id, "expose_protocol": ""})
        assert off.status_code == 200 and off.json()["bridged"] is False
        assert gateway.post("/v1/responses", json=request_payload()).status_code == 404

        # 再打开
        on = gateway.put("/admin/api/models", json={"route_id": route_id, "expose_protocol": "openai"})
        assert on.json()["bridged"] is True
        assert cands(gateway, "gpt-bridge")[0]["bridged"] is True


def test_upstream_path_is_not_double_prefixed(gateway):
    """描述符里的 path 带 /v1，而 endpoint() 自己会补 /v1 —— 不剥掉就拼出 /v1/v1/…。

    这个坑只在集成测试里露出来（单测碰不到 URL 拼接），所以在这里钉一条。
    """
    from gateway import protocols
    from gateway.proxy import _forward_path

    assert _forward_path(protocols.CHAT) == "/chat/completions"
    assert _forward_path(protocols.OPENAI) == "/responses"
    assert _forward_path(protocols.ANTHROPIC) == "/messages"


def test_unsupported_bridge_combination_is_refused_at_save_time(gateway):
    """没实现的组合在保存时就拒掉，别攒到第一个请求才报「不支持」。"""
    with MockUpstream("siteA") as chat_only, MockUpstream("siteB") as claude_only:
        chat_group = add_upstream(gateway, chat_only, "siteA", "openai-chat")
        claude_group = add_upstream(gateway, claude_only, "siteB", "anthropic")

        bad = gateway.post("/admin/api/models", json={
            "model_name": "m", "group_id": chat_group, "remote_model": "m",
            "expose_protocol": "anthropic",
        })
        assert bad.status_code == 400
        assert "openai-chat" in bad.json()["detail"]

        # 分组接口已经是目标协议（anthropic 暴露成 openai）—— 那也是没实现的组合
        bad2 = gateway.post("/admin/api/models", json={
            "model_name": "m2", "group_id": claude_group, "remote_model": "m2",
            "expose_protocol": "openai",
        })
        assert bad2.status_code == 400


def test_bridged_and_native_candidates_can_share_one_model_name(gateway):
    """决策 2：同一个模型名允许「原生 Responses 候选 + 桥接候选」混排成一条降级链。

    混排的前提是两者**有效协议相同**；有效协议不同（比如一个原生 openai-chat
    候选）就必须拒掉 —— 否则「这个名字在哪个接口下暴露」没有答案。
    """
    with MockUpstream("siteA") as chat_only, MockUpstream("siteB") as native_only:
        chat_group = add_upstream(gateway, chat_only, "siteA", "openai-chat")
        native_group = add_upstream(gateway, native_only, "siteB", "openai")

        add_route(gateway, "m", native_group, "gpt-5.6-luna")
        bridged = gateway.post("/admin/api/models", json={
            "model_name": "m", "group_id": chat_group, "remote_model": "deepseek-v4-pro",
            "expose_protocol": "openai",
        })
        assert bridged.status_code == 200, bridged.text
        assert [c["bridged"] for c in cands(gateway, "m")] == [False, True]

        # 原生 openai-chat 候选（不开桥接）暴露成 openai-chat，和链上的 openai 冲突
        clash = gateway.post("/admin/api/models", json={
            "model_name": "m", "group_id": chat_group, "remote_model": "other-chat-model",
        })
        assert clash.status_code == 409
        assert "openai" in clash.json()["detail"]


# ---------------------------------------------------------------- 小工具


class _mock:
    """MockUpstream 的简写（`with MockUpstream("siteA") as m:`）。"""

    def __init__(self, name: str) -> None:
        from helpers import MockUpstream

        self._cls = MockUpstream
        self._name = name
        self._inner = None

    def __enter__(self):
        self._inner = self._cls(self._name).__enter__()
        return self._inner

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _group_of(gateway, upstream_name: str) -> int:
    row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["name"] == upstream_name)
    assert len(row["groups"]) == 1, row
    return int(row["groups"][0]["id"])


def test_sse_capture_flag_dumps_both_sides_once(gateway):
    """data/capture-sse.flag：下一条流式请求的上游字节和客户端字节各存一份，抓完即焚。

    这是「真包对比」用的 —— 桥接造的事件序列和真上游的事件序列差在哪，就看这两份。
    """
    from gateway import config

    with MockUpstream("siteA") as mock:
        mock.script_chat(text="抓包验证", usage=chat_usage())
        bridge_setup(gateway, mock)
        (config.DATA_DIR / "capture-sse.flag").write_bytes(b"")

        with gateway.stream("POST", "/v1/responses", json=request_payload(stream=True)) as resp:
            client_bytes = b"".join(resp.iter_bytes())

        upstream = (config.DATA_DIR / "captured_sse_upstream.txt").read_bytes()
        client = (config.DATA_DIR / "captured_sse_client.txt").read_bytes()
        assert upstream.startswith(b"data: {"), "上游侧存的是原始 chat SSE"
        assert client == client_bytes, "客户端侧存的就是实际发出去的字节"
        assert b"response.output_text.delta" in client
        assert not (config.DATA_DIR / "capture-sse.flag").exists(), "flag 抓完就删"

        # 没有 flag 的下一条请求不再产生文件（覆盖旧文件也不会发生）
        with gateway.stream("POST", "/v1/responses", json=request_payload(stream=True)) as resp:
            b"".join(resp.iter_bytes())
        assert (config.DATA_DIR / "captured_sse_upstream.txt").read_bytes() == upstream
