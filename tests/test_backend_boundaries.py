"""畸形配置、极端计量和长时间失败的有界处理。"""
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
    spans = [failover.note_fail(1, 502) for _ in range(2000)]
    assert spans[:6] == [0, 90, 180, 360, 600, 600]
    assert all(span == failover.COOL_MAX for span in spans[4:])
    failover.note_ok(1)
    assert failover.note_fail(1, 502) == 0


@pytest.mark.parametrize("number", [b"9" * 4400, b"1000000000001"])
@pytest.mark.parametrize("extract,key", [(protocols.openai_usage, b"input_tokens"), (protocols.anthropic_usage, b"input_tokens"), (protocols.chat_usage, b"prompt_tokens")])
def test_unreasonable_usage_is_missing_and_other_fields_survive(number, extract, key):
    raw = b'{"' + key + b'":' + number + b',"output_tokens":7,"completion_tokens":7}'
    assert extract(raw, raw)[:2] == (None, 7)


def test_usage_still_accepts_large_valid_values():
    assert protocols.openai_usage(b"", b'{"input_tokens":1000000000,"output_tokens":0}')[:2] == (1000000000, 0)
