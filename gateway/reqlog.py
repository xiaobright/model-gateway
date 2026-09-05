from __future__ import annotations

import time

from . import config


def log(line: str) -> None:
    stamp = time.strftime("%m-%d %H:%M:%S")
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.DATA_DIR / "gateway.log", "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass
