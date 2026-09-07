from __future__ import annotations

import json
import ssl
from pathlib import Path

import httpx

from . import config, protocols

MODELS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# 两种接口的路径都在 /v1 底下（`/v1/responses`、`/v1/messages`），所以前缀只有一个
API_PREFIX = "/v1"

# 自签证书的 https 代理用 `#ca=<pem 路径>` 把签发的那张证书钉进信任列表
CA_PREFIX = "ca="


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
    headers = dict(proto.fingerprint)
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


def split_ca(egress: str) -> tuple[str, str]:
    """拆掉代理 URL 上的 `#ca=…` 尾巴，返回 (剥干净的 URL, 片段原文)。

    httpx 不认识这个片段，所以必须传给它之前剥掉。没有片段时第二项是空串。
    """
    base, _, frag = egress.partition("#")
    return (base, frag) if frag else (egress, "")


def ca_context(frag: str) -> ssl.SSLContext:
    """把 `#ca=` 指的那张证书钉进一个 ssl context。

    自签证书的 https 代理（VPS 上 gost 那扇门）用系统根证书验不过 —— 钉上签发它那张
    就能过，而系统根证书原样保留，不影响别的站。相对路径按仓库根解析，网关从哪个
    目录启动都一样。

    语法不对、文件不在，一律抛 ValueError。保存出口时和真正建 client 时都得走这个函数，
    所以「配错了」和「配错到什么程度」只有一份说法 —— 保存时就地报 400，别攒到第一个
    请求失败才发现。
    """
    if not frag.startswith(CA_PREFIX) or len(frag) == len(CA_PREFIX):
        raise ValueError(f"代理 URL 的 # 片段只认 ca=<证书路径>（收到 {frag!r}）")
    ca = Path(frag[len(CA_PREFIX):])
    if not ca.is_absolute():
        ca = config.PROJECT_ROOT / ca
    if not ca.is_file():
        raise ValueError(f"#ca 指的证书文件不存在：{ca}")
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ca))
    return ctx


async def fetch_remote_models(
    base_url: str, api_key: str, header_override: str = "", protocol: str = "openai",
    egress: str = "",
) -> tuple[str, ...]:
    # 出口和转发共用一套规则（回环直连、'direct' 连系统代理也关掉），理由见 proxy.py。
    # 这里必须也按出口走：不然「只能走代理才通」的站转发是好的、拉列表却失败，
    # 最容易被误判成 key 填错了
    from .proxy import client_args

    url = models_url(base_url)
    async with httpx.AsyncClient(timeout=MODELS_TIMEOUT, **client_args(egress)) as client:
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
