from __future__ import annotations

import json

import httpx

from . import protocols

MODELS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# 两种接口的路径都在 /v1 底下（`/v1/responses`、`/v1/messages`），所以前缀只有一个
API_PREFIX = "/v1"

# 有些站按客户端指纹拦截，默认就伪装成这个接口对应的官方客户端。
# 供应商自己的「请求头覆写」能改掉或删掉这里的任何一个头。
FINGERPRINTS = {
    "openai": {"user-agent": "codex_cli_rs", "originator": "codex_cli_rs"},
    "anthropic": {"user-agent": "claude-cli/2.0.0 (external, cli)", "x-app": "cli"},
}


def normalize_base(base_url: str) -> str:
    """库里存的是**站根**，不带 /v1。

    OpenAI 那边的客户端习惯让你填到 `/v1` 为止，Anthropic 那边让你填站根（它自己拼
    `/v1/messages`）—— 同一个站两种说法，说明 `/v1` 属于接口路径而不属于站点。所以
    统一剥掉尾部的 `/v1`，需要时由 endpoint() 补回来。
    """
    base = base_url.strip().rstrip("/")
    if base.lower().endswith(API_PREFIX):
        base = base[: -len(API_PREFIX)].rstrip("/")
    return base


def endpoint(base_url: str, path: str) -> str:
    """站根 + /v1 + 具体路径。base_url 里残留了 /v1 也不会拼出两个来。"""
    return normalize_base(base_url) + API_PREFIX + path


def models_url(base_url: str) -> str:
    return endpoint(base_url, "/models")


def build_headers(api_key: str, header_override: str = "", protocol: str = "openai") -> dict[str, str]:
    """拉模型列表用的头。鉴权头按接口给：Anthropic 站认 `x-api-key`，只发
    `Authorization` 的话多半是 401；`anthropic-version` 也是它那边的必需头。"""
    proto = protocols.by_name(protocol)
    # 键统一小写，否则覆写 "user-agent" 时会和 "User-Agent" 同时存在，httpx 会把两个都发出去
    headers = dict(FINGERPRINTS.get(protocol) or FINGERPRINTS["openai"])
    headers.update(proto.defaults)
    if api_key:
        headers.update(proto.auth_headers(api_key))
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


async def fetch_remote_models(
    base_url: str, api_key: str, header_override: str = "", protocol: str = "openai"
) -> tuple[str, ...]:
    from .proxy import loopback_mounts  # 回环地址不绕系统代理，理由见 proxy.py

    url = models_url(base_url)
    async with httpx.AsyncClient(timeout=MODELS_TIMEOUT, mounts=loopback_mounts()) as client:
        resp = await client.get(url, headers=build_headers(api_key, header_override, protocol))
    if resp.status_code != 200:
        raise RuntimeError(f"{url} 返回 {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{url} 返回的不是 JSON: {resp.text[:200]}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError(f"{url} 的响应里没有 data 数组")
    return tuple(str(m["id"]) for m in data if isinstance(m, dict) and "id" in m)
