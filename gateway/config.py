from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "gateway.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
WEB_DIR = PROJECT_ROOT / "web"

DEFAULT_PORT = 8317
DEFAULT_HOST = "127.0.0.1"


def load_port() -> int:
    try:
        raw = json.loads(SETTINGS_PATH.read_text("utf-8"))
        value = raw.get("port", DEFAULT_PORT)
        # JSON 里的 true 会被 int() 收成 1 并通过范围检查
        if isinstance(value, bool):
            return DEFAULT_PORT
        port = int(value)
    except (OSError, ValueError, TypeError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT
