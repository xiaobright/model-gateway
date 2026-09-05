from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

from gateway import config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Model Gateway")
    parser.add_argument("--port", type=int, default=config.load_port())
    parser.add_argument("--tray", action="store_true", help="以托盘模式运行（Windows）")
    parser.add_argument("--no-tray", action="store_true", help="前台控制台模式")
    parser.add_argument("--open", action="store_true", dest="open_ui", help="启动后打开管理页")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.tray and args.no_tray:
        print("--tray 与 --no-tray 互斥", file=sys.stderr)
        return 2
    use_tray = args.tray or (not args.no_tray and sys.platform == "win32")

    if use_tray:
        from gateway import tray as tray_mod
        from gateway import server as server_mod
        from gateway.server import start_server_thread

        if not tray_mod.acquire_single_instance():
            return 0

        server, thread = start_server_thread(args.port)
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        if not server.started:
            tray_mod.msgbox(f"启动失败：端口 {args.port} 可能被占用，或配置有误")
            return 1

        if args.open_ui:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")

        tray_app = tray_mod.TrayApp(server, args.port)
        server_mod.attach_tray(tray_app)
        tray_app.run()
        thread.join(timeout=15)
        os._exit(0)

    import uvicorn

    from gateway import server as server_mod
    from gateway.app import create_app

    srv = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=args.port, log_level="info"))
    server_mod.register_server(srv)
    srv.run()
    return 0


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback

        tb = traceback.format_exc()
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            (config.DATA_DIR / "crash.log").write_text(tb, encoding="utf-8")
        except OSError:
            pass
        try:
            from gateway.tray import msgbox

            msgbox("启动崩溃，详情见 data\\crash.log\n\n" + tb[-800:])
        except Exception:
            pass
        raise
