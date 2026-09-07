"""端到端测试 · 出口：每扇门各自通不通、自签证书的 https 代理、保存时就地校验。"""

from __future__ import annotations

import httpx
import pytest

from helpers import PROXY_CERT, PROXY_KEY, free_port, MockProxy, MockUpstream, add_upstream, add_route


# 这一整组都要等真实的 connect 超时 / 冷却期满，没法靠 mock 加速 ——
# 日常跑 pytest -m "not slow" 可以先跳过它们
pytestmark = pytest.mark.slow


# ================================================================ 出口（每个站从哪扇门出去）
#
# 现实里同一台机器上「有的站必须走代理、有的站必须别走代理」是常态：公益站按 IP 屏蔽，
# 而校园网 IP 和机房 IP 各自被不同的站拉黑。出口是**供应商**的属性，和 base_url 同一层。



def set_egress(client: httpx.Client, name: str, egress: str) -> None:
    row = next(u for u in client.get("/admin/api/upstreams").json() if u["name"] == name)
    resp = client.put(
        f"/admin/api/upstreams/{row['id']}",
        json={"name": row["name"], "base_url": row["base_url"], "enabled": True, "egress": egress},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["egress"] == egress


def test_disabling_a_provider_does_not_reset_its_egress(gateway):
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        set_egress(gateway, "siteA", "direct")
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["name"] == "siteA")

        # 模拟列表里的快捷开关：即使旧调用方没有带 egress，后端也保留它。
        changed = gateway.put(
            f"/admin/api/upstreams/{row['id']}",
            json={"name": row["name"], "base_url": row["base_url"], "enabled": False},
        )
        assert changed.status_code == 200 and changed.json()["egress"] == "direct"


def test_egress_sends_that_site_through_the_proxy(gateway):
    """填了代理的站，字节真的从那扇门出去；没填的站照旧不走。"""
    with MockProxy() as px, MockUpstream("siteA") as a, MockUpstream("siteB") as b:
        g_a = add_upstream(gateway, a, "siteA")
        g_b = add_upstream(gateway, b, "siteB")
        add_route(gateway, "via-proxy", g_a, "gpt-remote")
        add_route(gateway, "direct-one", g_b, "gpt-remote")
        set_egress(gateway, "siteA", px.url)

        assert gateway.post("/v1/responses", json={"model": "via-proxy"}).json()["upstream"] == "siteA"
        assert [u for u in px.seen if str(a.port) in u], f"代理没看到这个请求: {px.seen}"

        before = len(px.seen)
        assert gateway.post("/v1/responses", json={"model": "direct-one"}).json()["upstream"] == "siteB"
        assert len(px.seen) == before, "没配出口的站不该从代理走"

        # 拉模型列表也得按出口走，否则「转发好的、拉列表失败」会被当成 key 填错
        assert gateway.get(f"/admin/api/groups/{g_a}/remote-models").status_code == 200
        assert len([u for u in px.seen if "/v1/models" in u]) == 1
        assert gateway.get(f"/admin/api/groups/{g_b}/remote-models").status_code == 200
        assert len([u for u in px.seen if "/v1/models" in u]) == 1


def test_direct_really_turns_the_system_proxy_off():
    """「直连」必须连 trust_env 一起关掉。

    httpx 不只看 HTTP_PROXY 这类环境变量，在 Windows 上还会读注册表里的系统代理
    （Clash 那种），而注册表的 bypass 列表通常是空的。只把 proxy 设成 None 的话，
    「让这个站绕过代理」这件事根本没做到 —— 而这正是被机房 IP 拉黑的站唯一的出路。
    """
    from gateway import proxy as proxy_mod

    follow = proxy_mod.client_args("")
    direct = proxy_mod.client_args("direct")
    via = proxy_mod.client_args("http://127.0.0.1:7890")

    assert follow["trust_env"] is True and follow["proxy"] is None
    assert direct["trust_env"] is False and direct["proxy"] is None
    assert via["trust_env"] is False and via["proxy"] == "http://127.0.0.1:7890"
    # 回环 mounts 是用来抵消**隐式**的系统代理的，所以只在没指定代理时挂
    assert follow["mounts"] and direct["mounts"] and not via["mounts"]


def test_egress_only_takes_proxy_urls(gateway):
    """vless / ss 这类得先由本机内核落成一个 http/socks 端口，直接填进来只会在转发时才炸。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["name"] == "siteA")
        resp = gateway.put(
            f"/admin/api/upstreams/{row['id']}",
            json={"name": "siteA", "base_url": row["base_url"], "egress": "vless://whatever"},
        )
        assert resp.status_code == 400 and "http://" in resp.text


def test_probe_reports_which_door_works(gateway):
    """「测一下」：同一个站从每扇门各打一次。这个问题只能实测，猜不出来。"""
    with MockProxy() as px, MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["name"] == "siteA")
        set_egress(gateway, "siteA", px.url)

        data = gateway.post(f"/admin/api/upstreams/{row['id']}/probe").json()
        by_label = {r["label"]: r for r in data["results"]}
        assert set(by_label) == {"跟随系统", "直连", "这个代理"}
        assert all(r["ok"] and r["status"] == 200 for r in data["results"]), data
        assert data["current"] == px.url

        # 换成一个没人听的端口：那扇门报不通，另外两扇照旧通
        set_egress(gateway, "siteA", f"http://127.0.0.1:{free_port()}")
        data = gateway.post(f"/admin/api/upstreams/{row['id']}/probe").json()
        by_label = {r["label"]: r for r in data["results"]}
        assert by_label["这个代理"]["ok"] is False and by_label["这个代理"]["error"]
        assert by_label["直连"]["ok"] is True


def test_egress_ca_pin_works_end_to_end(gateway):
    """保存带 #ca 的出口，转发字节真的从那扇自签 TLS 门过。"""
    with MockProxy(certfile=PROXY_CERT, keyfile=PROXY_KEY) as px, MockUpstream("siteA") as a:
        g_a = add_upstream(gateway, a, "siteA")
        add_route(gateway, "via-vps", g_a, "gpt-remote")
        set_egress(gateway, "siteA", f"https://me:pw@127.0.0.1:{px.port}#ca={PROXY_CERT}")

        assert gateway.post("/v1/responses", json={"model": "via-vps"}).json()["upstream"] == "siteA"
        assert px.seen, f"代理没看到这个请求: {px.seen}"


def test_egress_ca_pin_is_checked_at_save_time(gateway):
    """#ca 的错误当场说清：片段不认识、socks5 没有证书可验、文件不在。"""
    with MockUpstream("siteA") as a:
        add_upstream(gateway, a, "siteA")
        row = next(u for u in gateway.get("/admin/api/upstreams").json() if u["name"] == "siteA")

        def put(egress: str) -> httpx.Response:
            return gateway.put(
                f"/admin/api/upstreams/{row['id']}",
                json={"name": "siteA", "base_url": row["base_url"], "enabled": True, "egress": egress},
            )

        assert put("https://me:pw@h:8443#foo=1").status_code == 400
        assert put(f"socks5://127.0.0.1:1080#ca={PROXY_CERT}").status_code == 400
        assert put("https://me:pw@h:8443#ca=data/no-such-ca.pem").status_code == 400

        ok = put(f"https://me:pw@h:8443#ca={PROXY_CERT}")
        assert ok.status_code == 200 and ok.json()["egress"].endswith(f"#ca={PROXY_CERT}")


def test_egress_vps_preset_comes_from_the_settings_table(gateway):
    """「走 VPS」这个预设是运维事实不是代码：值在设置表里，没配就没有这个选项。"""
    from gateway import db

    assert gateway.get("/admin/api/egress-presets").json() == {"vps": None}
    db.set_setting("egress_vps", "https://u:p@203.0.113.10:8443#ca=data/vps-proxy-ca.pem")
    assert gateway.get("/admin/api/egress-presets").json()["vps"].endswith(
        ":8443#ca=data/vps-proxy-ca.pem"
    )
