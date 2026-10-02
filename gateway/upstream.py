"""上游连接：地址、鉴权、出口/TLS、共享连接池和模型列表读取。"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import ssl
import sys
import urllib.request
from pathlib import Path

import httpx

from . import config, protocols

# connect 只给 8 秒：真库里 104 个 502 全是连不上，平均白等 16.6 秒（旧值是 15 秒的
# connect 超时在磨）。握手 8 秒都完不成的站，也扛不住几十万 token 的请求体。
PROXY_TIMEOUT = httpx.Timeout(connect=8.0, read=600.0, write=60.0, pool=600.0)
PROXY_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)

# 系统出口中的本机地址一律直连；显式指定代理不受此规则影响。
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

# 「出口」的两个特殊值，其余一律当代理 URL（http:// 或 socks5://）
EGRESS_SYSTEM = ""        # Windows 跟随系统开关；其他平台沿用 urllib 的代理来源
EGRESS_DIRECT = "direct"  # 直连：把系统代理也关掉

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
    try:
        ctx.load_verify_locations(cafile=str(ca))
    except (ssl.SSLError, OSError) as exc:
        # 文件在但内容不是 PEM 时抛的是 ssl.SSLError（OSError 子类，不是 ValueError）：
        # 统一包成 ValueError，保存出口和转发降级才共用同一种「配置坏了」的语义
        raise ValueError(f"#ca 指的证书读不出来（{ca}）：{exc}") from exc
    return ctx


def system_ssl_context() -> ssl.SSLContext | None:
    """建一个「跟系统证书库走」的 ssl context，杀软 MITM 也能验过。

    Windows 上卡巴斯基这类「加密连接扫描」会用自己的根证书重签所有 TLS 流量：
    根证书装在系统库里（curl/浏览器都认），但 httpx 默认用自带的 CA 捆绑包，
    不认 → CERTIFICATE_VERIFY_FAILED。truststore 把系统库注入 ssl，问题消失。
    没装 truststore（或平台不支持）返回 None，调用方保持 httpx 默认行为。
    """
    try:
        import truststore
    except ImportError:
        return None
    try:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


ProxySnapshot = tuple[tuple[str, str], ...]


def _system_proxy_signature() -> ProxySnapshot:
    """一次读取的配置同时用于缓存键与 transport，禁止 httpx 再读取环境。"""
    proxies = (
        urllib.request.getproxies_registry()
        if sys.platform == "win32" else urllib.request.getproxies()
    )
    return tuple(sorted(
        (str(key).lower(), str(value)) for key, value in proxies.items()
        if str(key).lower() in {"http", "https", "all", "no"}
    ))


def _proxy_patterns(snapshot: ProxySnapshot) -> dict[str, str | None]:
    """将固定快照转换为 httpx 的公开 mounts 配置，保留 NO_PROXY 语义。"""
    proxies = dict(snapshot)
    mounts: dict[str, str | None] = {}
    for scheme in ("http", "https", "all"):
        if value := proxies.get(scheme):
            mounts[f"{scheme}://"] = value if "://" in value else f"http://{value}"
    for host in proxies.get("no", "").split(","):
        host = host.strip()
        if not host:
            continue
        if host == "*":
            return {}
        if "://" in host:
            mounts[host] = None
            continue
        try:
            address = ipaddress.ip_address(host.split("/")[0])
        except ValueError:
            pattern = host if host.lower() == "localhost" or host.startswith("*") else f"*{host}"
        else:
            pattern = f"[{host}]" if address.version == 6 else host
        mounts[f"all://{pattern}"] = None
    return mounts


def loopback_mounts() -> dict[str, None]:
    # None 选择 client 自带的直连 transport，复用其证书和连接池参数。
    return {f"all://{host}": None for host in LOOPBACK}


def client_args(egress: str, *, system_proxy: ProxySnapshot | None = None) -> dict:
    """按「出口」拼出建 client 要的那几个参数。

    这是整件事唯一的开关：网关自己就是发请求的那个客户端，socket 是它自己开的，
    所以按站换出口不需要任何代理内核 —— 内核的存在意义是替「不知道有代理」的进程
    做拦截。

    所有出口均禁用 httpx 隐式读取代理。跟随系统时显式应用固定快照：Windows
    只跟随注册表开关，不受启动时继承的 HTTP(S)_PROXY 等环境变量覆盖。

    回环 mounts 只在没指定代理时挂：它是用来抵消系统代理的（否则连本机
    上游都要绕一趟 Clash）。明确给某个站指了代理，就按说的走 —— 真实场景里没人会给
    127.0.0.1 的站配代理，而测试要的正是「字节真的从那扇门出去了」。

    代理 URL 允许带 `#ca=<pem 路径>` 的尾巴（自签证书的 https 代理，VPS 上 gost 那扇门）。
    怎么拆、怎么校验都在 upstream 里（split_ca / ca_context）—— 保存出口时和真正建 client
    时走的是同一段代码，别在两处各写一份规则。
    """
    egress = (egress or "").strip()
    if egress == EGRESS_SYSTEM and system_proxy is None:
        system_proxy = _system_proxy_signature()
    proxy = None if egress in (EGRESS_SYSTEM, EGRESS_DIRECT) else egress
    args: dict = {
        "mounts": {} if proxy else loopback_mounts(),
        "trust_env": False,
        "proxy": proxy,
    }
    # 用 Windows 系统证书库验证上游 TLS。httpx 默认用自带的 CA 捆绑包，认不得
    # 卡巴斯基这类「加密连接扫描」的 MITM 根证书 —— 明明 curl 能通、浏览器能通，
    # 网关却 502 "self-signed certificate in certificate chain"（2026-09-11 实锤）。
    # truststore 把系统库注入 ssl：杀软的根是用户自己机器上受信的，跟着走。
    # 没装 truststore 就退回 httpx 默认行为。
    ctx = system_ssl_context()
    if ctx is not None:
        args["verify"] = ctx
    if egress == EGRESS_SYSTEM:
        patterns = _proxy_patterns(system_proxy or ())
        patterns.update(loopback_mounts())
        args["mounts"] = {
            pattern: httpx.AsyncHTTPTransport(
                proxy=url, verify=ctx if ctx is not None else True,
                trust_env=False, limits=PROXY_LIMITS,
            ) if url else None
            for pattern, url in patterns.items()
        }
    if proxy:
        base, frag = split_ca(proxy)
        if frag:
            # httpx 连代理这一跳用的是 Proxy 对象上单独的 ssl_context，client 的
            # verify 管不到它 —— 钉证书必须钉在这里
            args["proxy"] = httpx.Proxy(httpx.URL(base), ssl_context=ca_context(frag))
    return args


_clients: dict[object, httpx.AsyncClient] = {}
_client_loop: asyncio.AbstractEventLoop | None = None
_retired_clients: set[_SystemClient] = set()


class _SystemStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, client: _SystemClient) -> None:
        self.stream = stream
        self.client = client
        self.closed = False

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream.aclose()
        finally:
            await self.client.release()


class _SystemClient(httpx.AsyncClient):
    """退役只禁止新请求；发送中和已返回响应头的流都计入占用。"""

    retired = False
    active = 0

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        if self.retired:
            # get_client 返回后、send 真正执行前，另一个请求可能已切换出口。
            current = await get_client()
            return await current.send(request, **kwargs)
        self.active += 1
        try:
            response = await super().send(request, **kwargs)
        except BaseException:
            await self.release()
            raise
        if response.is_closed:
            await self.release()
        else:
            response.stream = _SystemStream(response.stream, self)
        return response

    async def release(self) -> None:
        self.active -= 1
        await self.close_if_idle()

    async def close_if_idle(self) -> None:
        if self.retired and self.active == 0:
            try:
                await self.aclose()
            finally:
                _retired_clients.discard(self)


async def get_client(egress: str = EGRESS_SYSTEM) -> httpx.AsyncClient:
    """按「出口」复用 client，省掉每个请求一次 TLS 握手（对远端公益站是几百 ms 的差别）。

    同一个出口和同一份系统代理配置复用一个 client。代理是建 client 时定的，没法按请求
    换；系统代理开关变化后用同一份快照建新 client，旧 client 待在途响应关闭后回收。
    """
    global _client_loop
    loop = asyncio.get_running_loop()
    if _client_loop is not loop:
        # 换 loop 了（托盘模式下服务跑在另一个线程里）。旧 client 的连接池绑着上一个
        # loop，留着就是泄漏一批连接和 fd —— 而且它们已经没人能用了。
        # 先占坑再清理：否则两个并发请求同时进这段，后完成者会把先完成者刚建的
        # client 一起清掉（那段连接池就没人关了）
        _client_loop = loop
        await aclose_client()
    egress = (egress or "").strip()
    snapshot = _system_proxy_signature() if egress == EGRESS_SYSTEM else None
    cache_key: object = (EGRESS_SYSTEM, snapshot) if snapshot is not None else egress
    client = _clients.get(cache_key)
    if client is None or client.is_closed:
        client_type = _SystemClient if snapshot is not None else httpx.AsyncClient
        client = client_type(
            timeout=PROXY_TIMEOUT, limits=PROXY_LIMITS,
            **client_args(egress, system_proxy=snapshot)
        )
        _clients[cache_key] = client
    if snapshot is not None:
        retired = []
        for key, old in list(_clients.items()):
            if key != cache_key and isinstance(old, _SystemClient):
                del _clients[key]
                old.retired = True
                _retired_clients.add(old)
                retired.append(old)
        for old in retired:
            with contextlib.suppress(Exception):
                await old.close_if_idle()
    return client


async def aclose_client() -> None:
    # 先摘快照再关：并发 get_client 在等待期间新建的 client 不能被后到的清理扫掉。
    # 不在这里改 _client_loop —— 谁切换 loop 谁负责记，清理只负责关连接。
    snapshot = list(_clients.items())
    retired = list(_retired_clients)
    _retired_clients.clear()
    for key, client in snapshot:
        if _clients.get(key) is client:
            del _clients[key]
    for client in [c for _, c in snapshot] + retired:
        if not client.is_closed:
            with contextlib.suppress(Exception):
                await client.aclose()


async def fetch_remote_models(
    base_url: str, api_key: str, header_override: str = "", protocol: str = "openai",
    egress: str = "",
) -> tuple[str, ...]:
    # 出口和转发共用一套规则（回环直连、'direct' 连系统代理也关掉），规则见本模块的 client_args()。
    # 这里必须也按出口走：不然「只能走代理才通」的站转发是好的、拉列表却失败，
    # 最容易被误判成 key 填错了
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
