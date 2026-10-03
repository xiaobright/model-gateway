from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from gateway import config
from gateway.startup import StartupError, configure_logging, logger


def _port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是整数")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口要在 1-65535 之间")
    return port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Model Gateway")
    parser.add_argument("--port", type=_port_arg, default=config.load_port())
    parser.add_argument("--tray", action="store_true", help="以托盘模式运行（Windows）")
    parser.add_argument("--no-tray", action="store_true", help="前台控制台模式")
    parser.add_argument("--open", action="store_true", dest="open_ui", help="启动后打开管理页")
    return parser.parse_args()


def main() -> int:
    log_path = configure_logging()
    logger.info("网关进程启动 executable=%s", sys.executable)
    args = parse_args()
    if args.tray and args.no_tray:
        print("--tray 与 --no-tray 互斥", file=sys.stderr)
        return 2
    use_tray = args.tray or (not args.no_tray and sys.platform == "win32")

    if use_tray:
        from gateway import tray as tray_mod
        from gateway import server as server_mod
        from gateway.server import start_server_thread, wait_for_startup

        if not tray_mod.acquire_single_instance():
            logger.info("已有托盘实例，当前进程退出")
            return 0

        server = thread = None
        try:
            server, thread = start_server_thread(args.port)
            wait_for_startup(server, thread)
        except StartupError as exc:
            logger.error("启动未完成 kind=%s: %s", exc.kind, exc)
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=5)
            detail = f"详情见 {log_path}" if log_path is not None else "启动日志无法写入，请检查 data 目录权限。"
            tray_mod.msgbox(f"{exc.message(args.port)}\n\n{detail}")
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
        logger.exception("网关主线程异常")
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
