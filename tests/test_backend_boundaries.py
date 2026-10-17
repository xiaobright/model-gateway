"""畸形配置、极端计量和长时间失败的有界处理。"""
import json

import pytest

from gateway import config, failover, protocols


@pytest.mark.parametrize("raw", ['[]', 'null', '"text"', '{"port":1e999}', '{"port":true}', '{"port":9000.5}'])
def test_invalid_port_shape_uses_the_default(tmp_path, monkeypatch, raw):
    path = tmp_path / "settings.json"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    assert config.load_port() == config.DEFAULT_PORT


def test_cooldown_stays_capped_after_thousands_of_failures(monkeypatch):
    monkeypatch.setattr(failover, "_states", {})
    monkeypatch.setattr(failover, "log", lambda *args: None)
    spans = [failover.note_fail(1, "m", 502) for _ in range(2000)]
    assert spans[:6] == [0, 90, 180, 360, 600, 600]
    assert all(span == failover.COOL_MAX for span in spans[4:])
    failover.note_ok(1, "m")
    assert failover.note_fail(1, "m", 502) == 0


def test_cooldown_is_per_upstream_model_within_a_group(monkeypatch):
    """同一个分组下，一个真名连败进冷却，兄弟真名必须毫发无伤。"""
    monkeypatch.setattr(failover, "_states", {})
    monkeypatch.setattr(failover, "log", lambda *args: None)
    for _ in range(failover.COOL_AFTER):
        failover.note_fail(7, "sick-name", 503)
    assert failover.cooling(7, "sick-name") > 0
    assert failover.cooling(7, "healthy-name") == 0, "同组的兄弟真名不该被连带冷却"
    failover.note_ok(7, "healthy-name")
    assert failover.cooling(7, "sick-name") > 0, "兄弟真名成功不该清掉别人的冷却"


@pytest.mark.parametrize("number", [b"9" * 4400, b"1000000000001"])
@pytest.mark.parametrize("extract,key", [(protocols.openai_usage, b"input_tokens"), (protocols.anthropic_usage, b"input_tokens"), (protocols.chat_usage, b"prompt_tokens")])
def test_unreasonable_usage_is_missing_and_other_fields_survive(number, extract, key):
    raw = b'{"' + key + b'":' + number + b',"output_tokens":7,"completion_tokens":7}'
    assert extract(raw, raw)[:2] == (None, 7)


def test_usage_still_accepts_large_valid_values():
    assert protocols.openai_usage(b"", b'{"input_tokens":1000000000,"output_tokens":0}')[:2] == (1000000000, 0)


# DDD（api.dddai.dev）2026-10-16 起：response.completed 的 usage 里带 attribution
# （按输入项、按请求字段拆的明细），tool_usage 是 usage 的兄弟节点，每个明细内部
# 又有同名的 token 数字。线上实测：顶层真数 4388/6/缓存 4224，修前记成 0/0/4096。
_REAL_FIELDS = (
    b'"input_tokens":4388,"input_tokens_details":{"cached_tokens":4224,"cache_write_tokens":0},'
    b'"output_tokens":6,"output_tokens_details":{"reasoning_tokens":0},"total_tokens":4394'
)
_TOOL_USAGE = (
    b'"tool_usage":{"image_gen":{"input_tokens":0,"output_tokens":0,"total_tokens":0,'
    b'"input_tokens_details":{"text_tokens":0,"image_tokens":0}},'
    b'"web_search":{"num_requests":0}}'
)


def _completed_event(usage_inner: bytes) -> bytes:
    return (
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"id":"resp_x",'
        b'"usage":{' + usage_inner + b"}," + _TOOL_USAGE + b',"store":false}}\n\n'
    )


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": "), (",", " : \t")])
@pytest.mark.parametrize("details_first", [False, True])
def test_openai_usage_uses_paths_with_json_whitespace_and_nested_usage(separators, details_first):
    fields = json.loads(b"{" + _REAL_FIELDS + b"}")
    details = {"attribution": {"usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}},
               "unknown_detail": {"input_tokens": 1, "output_tokens": 2}}
    usage = {**details, **fields} if details_first else {**fields, **details}
    payload = {"type": "response.completed", "response": {"usage": usage},
               "tool_usage": {"input_tokens": 0, "output_tokens": 0}}
    raw = json.dumps(payload, separators=separators).encode()
    assert protocols.openai_usage(b"", raw) == (4388, 6, 4224, None)
    assert protocols.openai_usage(b"", b"data: " + raw + b"\n\n") == (4388, 6, 4224, None)


def test_partial_usage_window_does_not_mistake_details_for_totals():
    raw = _completed_event(_REAL_FIELDS + b',"attribution":{"padding":"' + b"x" * 70000
                           + b'","input_tokens":0,"output_tokens":0,"cached_tokens":0}')
    assert protocols.openai_usage(b"", raw[-65536:]) == (None, None, None, None)
    observer = protocols.SSEObserver(protocols.OPENAI)
    for start in range(0, len(raw), 4093):
        observer.feed(raw[start:start + 4093])
    assert observer.ended
    assert observer.usage == (4388, 6, 4224, None)


def test_openai_usage_never_reads_body_text_or_arbitrary_nested_objects():
    raw = json.dumps({"output": [{"usage": {"input_tokens": 123, "output_tokens": 9}}]}).encode()
    assert protocols.openai_usage(b"", raw) == (None, None, None, None)
    assert protocols.openai_payload_usage({"usage": {"input_tokens": True, "output_tokens": -1,
                                                   "input_tokens_details": {"cached_tokens": 1.5}}}) == (None, None, None, None)
