"""一次性诊断：看 DDD 现在实际返回的 usage 块长什么样。

从正式库读分组 key（不打印），直连上游发一个最小流式请求，把 response.completed
的 usage 原样打出来。用来判断「统计里没用量」是上游没报，还是报了但形状变了。
每次运行花一点点额度；key 只在进程内使用，不落盘、不进日志。

    uv run python dev/probe-ddd-usage.py [模型名]
"""
import json
import sqlite3
import sys

import httpx

conn = sqlite3.connect("file:data/gateway.db?mode=ro", uri=True)
key = conn.execute("SELECT api_key FROM upstream_groups WHERE id=2").fetchone()[0]
base = conn.execute("SELECT base_url FROM upstreams WHERE id=2").fetchone()[0]
model = sys.argv[1] if len(sys.argv) > 1 else "gpt-6.1-sol"

body = {
    "model": model,
    "stream": True,
    "store": False,
    "max_output_tokens": 16,
    "input": [{"role": "user", "content": "say hi"}],
}
buf = b""
with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
    with client.stream(
        "POST",
        f"{base}/v1/responses",
        json=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    ) as resp:
        print("HTTP", resp.status_code, "content-type:", resp.headers.get("content-type"))
        for chunk in resp.iter_bytes():
            buf += chunk

events = []
for block in buf.split(b"\n\n"):
    for line in block.split(b"\n"):
        if line.startswith(b"data:"):
            payload = line[5:].strip()
            if payload == b"[DONE]":
                events.append(("[DONE]", None))
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            events.append((obj.get("type"), obj))

def _usage_of(obj):
    """usage 可能在事件顶层，也可能在 response.completed 的 response 里。"""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("usage"), dict):
        return obj["usage"]
    response = obj.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return response["usage"]
    return None


print(f"总字节 {len(buf)}，事件 {len(events)} 个")
print("事件类型序列:", [t for t, _ in events])

for t, obj in events:
    if obj is not None and "usage" in json.dumps(obj):
        print(f"\n--- {t} 的 usage ---")
        print(json.dumps(_usage_of(obj), indent=2, ensure_ascii=False))
