from __future__ import annotations

import json

import httpx

MODELS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# 有些站会按客户端指纹拦截，默认伪装成 codex CLI；上游自己的「请求头覆写」可以改掉或删掉这两个
DEFAULT_HEADERS = {"user-agent": "codex_cli_rs", "originator": "codex_cli_rs"}


def models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def build_headers(api_key: str, header_override: str = "") -> dict[str, str]:
    # 键统一小写，否则覆写 "user-agent" 时会和 "User-Agent" 同时存在，httpx 会把两个都发出去
    headers = dict(DEFAULT_HEADERS)
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    for key, value in parse_override(header_override).items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers


def parse_override(raw: str) -> dict[str, str | None]:
    """把「请求头覆写」JSON 解析成 {小写头名: 值 or None}；不合法就当没配。"""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k).lower(): v for k, v in parsed.items() if isinstance(v, (str, type(None)))}


async def fetch_remote_models(base_url: str, api_key: str, header_override: str = "") -> tuple[str, ...]:
    from .proxy import loopback_mounts  # 回环地址不绕系统代理，理由见 proxy.py

    async with httpx.AsyncClient(timeout=MODELS_TIMEOUT, mounts=loopback_mounts()) as client:
        resp = await client.get(models_url(base_url), headers=build_headers(api_key, header_override))
    if resp.status_code != 200:
        raise RuntimeError(f"上游返回 {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"上游返回的不是 JSON: {resp.text[:200]}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("上游 /models 响应里没有 data 数组")
    return tuple(str(m["id"]) for m in data if isinstance(m, dict) and "id" in m)
