"""工作台验收：每次新建独立数据库，所有 httpx 出站请求使用模拟传输。

uv run python dev/preview-workbench.py --port 8341 [--empty]
数据保留在已忽略的 dev/.preview/workbench-*，不清理已有目录。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def seed_preview(db) -> None:
    """造一批覆盖各种状态的预览数据：多候选、单候选、停用的站/分组、纯转发链。"""
    db.init_db()
    # 三个站：一个正常、一个多候选里当备用、一个停用
    a = db.create_upstream("站点A", "https://a.example.invalid")
    b = db.create_upstream("站点B", "https://b.example.invalid")
    c = db.create_upstream("站点C（停用）", "https://c.example.invalid", enabled=False)

    g_a = db.create_group(a.id, "默认", "openai")
    g_a2 = db.create_group(a.id, "备用key", "openai")
    g_b = db.create_group(b.id, "默认", "openai")
    g_c = db.create_group(c.id, "默认", "openai")
    g_an = db.create_group(a.id, "claude", "anthropic")
    g_bn = db.create_group(b.id, "claude", "anthropic")
    g_chat = db.create_group(b.id, "chat", "openai-chat")

    # 上游模型目录：拉取列表里能选的东西
    db.add_group_models(g_a.id, ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.4", "o5-mini"])
    db.add_group_models(g_a2.id, ["gpt-5.6-luna", "gpt-5.4"])
    db.add_group_models(g_b.id, ["deepseek-v3", "gpt-5.6-luna", "qwen3-max"])
    db.add_group_models(g_c.id, ["gpt-5.6-luna", "gpt-5.4"])
    db.add_group_models(g_an.id, ["claude-opus-4-5", "claude-sonnet-4-5"])
    db.add_group_models(g_bn.id, ["claude-opus-4-5"])
    db.add_group_models(g_chat.id, ["deepseek-v3", "qwen3-max"])

    # 一个候选：单链
    db.add_model_route("fast-chat", g_a.id, "gpt-5.6-luna")
    db.switch_route(db.list_routes()[0]["route_id"])

    # 三个候选
    for gid, remote in [(g_b.id, "deepseek-v3"), (g_a.id, "gpt-5.6-luna"), (g_c.id, "gpt-5.4")]:
        db.add_model_route("my-chat", gid, remote)

    # 两个候选，含 1M 后缀：真名和模型名不一样时标签要能区分
    db.add_model_route("long-chat", g_an.id, "claude-opus-4-5[1m]")
    db.add_model_route("long-chat", g_bn.id, "claude-opus-4-5")

    # 五个候选
    for gid, remote in [
        (g_a.id, "gpt-5.6-sol"), (g_a.id, "gpt-5.4"), (g_a.id, "o5-mini"),
        (g_a2.id, "gpt-5.6-luna"), (g_b.id, "qwen3-max"),
    ]:
        db.add_model_route("wide-chat", gid, remote)

    # Chat Completions 下另外一条链（同名模型多接口，接口是它自己的属性）
    db.add_model_route("my-chat", g_chat.id, "deepseek-v3")

    # 纯转发：自己没有候选，整条链交给 my-chat；再让 alias2 接到 alias 上，成一条链
    db.add_model_route("target-chat", g_a.id, "gpt-5.6-luna")
    db.set_forward("alias-chat", "openai", "my-chat")
    db.set_forward("alias2-chat", "openai", "alias-chat")

    # 停用分组：候选还在，但列表里该是灰的
    db.set_group_enabled(g_c.id, False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8341)
    parser.add_argument('--empty', action='store_true')
    args = parser.parse_args()
    os.environ['MODEL_GATEWAY_LEARNING'] = '0'

    from gateway import config, db, inflight
    import httpx
    import uvicorn

    parent = ROOT / 'dev' / '.preview'
    parent.mkdir(exist_ok=True)
    preview = Path(tempfile.mkdtemp(prefix='workbench-', dir=parent))
    config.DATA_DIR = preview
    config.DB_PATH = preview / 'gateway.db'
    config.SETTINGS_PATH = preview / 'settings.json'

    # 覆盖所有 httpx 客户端的 transport，目录拉取/探测/转发都不访问外部站点。
    # mounts 也要一起清掉：本机开着系统代理时 client_args 会挂上「全走代理」的
    # mounts，它比默认 transport 先匹配，模拟就被绕过去了（请求真的出网卡）。
    client_type = httpx.AsyncClient
    def mock_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': name} for name in
                ['gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.4', 'o5-mini',
                 'deepseek-v3', 'qwen3-max', 'claude-opus-4-5', 'claude-sonnet-4-5', 'preview-model']]})
        return httpx.Response(501, json={'error': {'message': 'isolated preview: mocked upstream'}})

    class PreviewClient(client_type):
        def __init__(self, *a, **kw):
            kw.update(transport=httpx.MockTransport(mock_response), trust_env=False, proxy=None, mounts={})
            super().__init__(*a, **kw)
    httpx.AsyncClient = PreviewClient

    if args.empty:
        db.init_db()
    else:
        seed_preview(db)
        db.add_model_route('unavailable-chat', 4, 'gpt-5.4')
        for i, note in enumerate(['ok', 'truncated', 'hold_retry', 'client_abort', 'manual_abort', 'connect_failed']):
            db.insert_request(client='Preview Client', model='my-chat', upstream='站点A',
                status=200 if i < 5 else 502, stream=True, req_bytes=2400, resp_bytes=1600,
                duration_ms=1200 + i * 730, input_tokens=600, output_tokens=120,
                cached_tokens=200, note=note, remote_model='gpt-5.6-luna',
                protocol='openai', group_name='默认', attempt=2 if note == 'hold_retry' else 1)
        for name, phase in [('my-chat', 'wait'), ('long-chat', 'stream')]:
            call = inflight.begin(client='Preview Client', protocol='openai' if name == 'my-chat' else 'anthropic',
                model=name, stream=True, req_bytes=2400)
            inflight.set_route(call, attempt=1, upstream='站点A', group_name='默认',
                group_id=1, remote_model='gpt-5.6-luna' if name == 'my-chat' else 'claude-opus-4-5[1m]', req_bytes=2400)
            inflight.phase(call, phase, status=200)
            if phase == 'stream':
                inflight.progress(call, 1600, text_bytes=400)

    from gateway.app import create_app
    config.set_bind('127.0.0.1', args.port)   # 和正式启动一样，让 /access-info 报对端口
    print(f'Isolated preview: http://127.0.0.1:{args.port}/ | DB: {config.DB_PATH}', flush=True)
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
