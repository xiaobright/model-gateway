"""工作台验收：每次新建独立数据库，所有 httpx 出站请求使用模拟传输。

uv run python dev/preview-workbench.py --port 8341 [--empty]
数据保留在已忽略的 dev/.canvas-preview/workbench-*，不清理已有目录。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8341)
    parser.add_argument('--empty', action='store_true')
    args = parser.parse_args()
    os.environ['MODEL_GATEWAY_LEARNING'] = '0'

    from gateway import config, db, inflight
    import httpx
    import uvicorn

    parent = ROOT / 'dev' / '.canvas-preview'
    parent.mkdir(exist_ok=True)
    preview = Path(tempfile.mkdtemp(prefix='workbench-', dir=parent))
    config.DATA_DIR = preview
    config.DB_PATH = preview / 'gateway.db'
    config.SETTINGS_PATH = preview / 'settings.json'

    # 覆盖所有 httpx 客户端的 transport，目录拉取/探测/转发都不访问外部站点。
    client_type = httpx.AsyncClient
    def mock_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': name} for name in
                ['gpt-5.6-luna', 'gpt-5.6-sol', 'gpt-5.4', 'o5-mini',
                 'deepseek-v3', 'qwen3-max', 'claude-opus-4-5', 'claude-sonnet-4-5', 'preview-model']]})
        return httpx.Response(501, json={'error': {'message': 'isolated preview: mocked upstream'}})

    class PreviewClient(client_type):
        def __init__(self, *a, **kw):
            kw.update(transport=httpx.MockTransport(mock_response), trust_env=False, proxy=None)
            super().__init__(*a, **kw)
    httpx.AsyncClient = PreviewClient

    if args.empty:
        db.init_db()
    else:
        spec = importlib.util.spec_from_file_location('canvas_preview', ROOT / 'dev' / 'preview-canvas.py')
        seed_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed_module)
        seed_module.seed()
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
    print(f'Isolated preview: http://127.0.0.1:{args.port}/ | DB: {config.DB_PATH}', flush=True)
    uvicorn.run(create_app(), host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
