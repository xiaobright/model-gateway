"""运行独立的编排预览：临时数据库、示例站点、不读取真实密钥或路由配置。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import config, db, failover
from gateway.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8329)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="gateway-liquid-preview-") as temporary:
        config.DATA_DIR = Path(temporary)
        config.DB_PATH = config.DATA_DIR / "gateway.db"
        config.SETTINGS_PATH = config.DATA_DIR / "settings.json"
        app = create_app()
        groups = {}
        for name in ["站C", "站I", "站D", "站A", "站E", "站F", "站B", "seeka"]:
            site = db.create_upstream(name, "https://" + name.lower() + ".example.invalid", enabled=name != "seeka")
            for protocol in ("openai", "anthropic"):
                groups[(name, protocol)] = db.create_group(site.id, "主分组" if protocol == "openai" else "Claude", protocol).id
        names = [
            ("gpt-6-astra", "openai", 6), ("gpt-5.6-sol", "openai", 7),
            ("gpt-5.6-terra", "openai", 7), ("gpt-5.6-luna", "openai", 4),
            ("claude-fable-5", "anthropic", 6), ("claude-opus-5", "anthropic", 5),
            ("claude-opus-4-8", "anthropic", 4), ("claude-sonnet-5", "anthropic", 3),
        ]
        sites = ["站C", "站I", "站D", "站A", "站F", "站B", "seeka"]
        for name, protocol, count in names:
            for i, site in enumerate(sites[:count]):
                remote = name + ("[1m]" if protocol == "anthropic" and i % 2 else "")
                db.add_model_route(name, groups[(site, protocol)], remote)
        failover.set_enabled("openai", True)
        failover.set_enabled("anthropic", True)
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
