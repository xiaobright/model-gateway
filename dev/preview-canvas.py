"""编排画布的本地预览：临时库 + 假站点，不碰日常那份配置。

用法（在项目目录）：:

    .venv\\Scripts\\python.exe dev/preview-canvas.py --port 8331

它会：
  - 把 config.DATA_DIR / DB_PATH 指到 dev/.canvas-preview/ 下的临时库；
  - 造几个不存在的站点和一批模型路由，覆盖画布要画的各种状态：
    多候选、单候选、停用的站、停用的分组、纯转发模型、成链的转发；
  - 起一个独立的服务进程。

**假站点不会真的被请求**，这里只验收画布本身，所以不需要它们在线。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import config  # noqa: E402

PREVIEW_DIR = Path(__file__).resolve().parent / ".canvas-preview"


def seed() -> None:
    from gateway import db

    db.init_db()

    # 三个站：一个正常、一个多候选里当备用、一个停用（画布上该灰掉）
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

    # 上游模型目录：上游池里能拖的东西
    db.add_group_models(g_a.id, ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.4", "o5-mini"])
    db.add_group_models(g_a2.id, ["gpt-5.6-luna", "gpt-5.4"])
    db.add_group_models(g_b.id, ["deepseek-v3", "gpt-5.6-luna", "qwen3-max"])
    db.add_group_models(g_c.id, ["gpt-5.6-luna", "gpt-5.4"])
    db.add_group_models(g_an.id, ["claude-opus-4-5", "claude-sonnet-4-5"])
    db.add_group_models(g_bn.id, ["claude-opus-4-5"])
    db.add_group_models(g_chat.id, ["deepseek-v3", "qwen3-max"])

    # 一个候选：看单端口的环长什么样
    db.add_model_route("fast-chat", g_a.id, "gpt-5.6-luna")
    db.switch_route(db.list_routes()[0]["route_id"])

    # 三个候选：看轮盘的顺序和拨动
    for gid, remote in [(g_b.id, "deepseek-v3"), (g_a.id, "gpt-5.6-luna"), (g_c.id, "gpt-5.4")]:
        db.add_model_route("my-chat", gid, remote)

    # 两个候选，含 1M 后缀：真名和模型名不一样时端口标签要能区分
    db.add_model_route("long-chat", g_an.id, "claude-opus-4-5[1m]")
    db.add_model_route("long-chat", g_bn.id, "claude-opus-4-5")

    # 五个候选：环应该被撑大
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

    # 停用分组：候选还在，但画布上该是虚线灰的
    db.set_group_enabled(g_c.id, False)


def main() -> int:
    parser = argparse.ArgumentParser(description="编排画布本地预览")
    parser.add_argument("--port", type=int, default=8331)
    parser.add_argument("--keep", action="store_true", help="保留上一次的临时库（默认每次重建）")
    parser.add_argument("--empty", action="store_true",
                        help="不造数据：用来看「全新用户第一次打开」的空画布长什么样")
    args = parser.parse_args()

    if PREVIEW_DIR.exists() and not args.keep:
        shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    # 必须在 create_app 之前改，config 是模块级的
    config.DATA_DIR = PREVIEW_DIR
    config.DB_PATH = PREVIEW_DIR / "preview.db"
    if args.empty:
        from gateway import db
        db.init_db()
    else:
        seed()

    from gateway.server import start_server_thread

    server, _thread = start_server_thread(args.port)
    import time
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.1)

    print(f"编排画布预览：http://127.0.0.1:{args.port}/#canvas")
    print("（临时库：" + str(PREVIEW_DIR) + "，Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
