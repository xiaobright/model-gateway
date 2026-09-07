from __future__ import annotations

import time
from pathlib import Path

from . import config

LOG_NAME = "gateway.log"

# 写满了就把当前的滚成 .1（原来的 .1 变 .2），重新开一个。
# 一天几百行，5MB 是好几周的量；留两份是为了「上周那次故障」还翻得到。
# 以前只追加不轮转，于是一个挂了几个月的服务会在 data 里攒出几百 MB 没人看的东西
MAX_BYTES = 5 * 1024 * 1024
KEEP = 2


def _at(index: int) -> Path:
    return config.DATA_DIR / (LOG_NAME if index == 0 else f"{LOG_NAME}.{index}")


def _rotate() -> None:
    """把已有的日志依次往后推一格，最老那份丢掉。"""
    for i in range(KEEP, 0, -1):
        if i == KEEP:
            _at(i).unlink(missing_ok=True)
        if _at(i - 1).exists():
            _at(i - 1).replace(_at(i))


def log(line: str) -> None:
    stamp = time.strftime("%m-%d %H:%M:%S")
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        target = _at(0)
        # 每次写之前量一下：一天也就几百次 stat，比自己记字节数省心，也不会记错
        if target.exists() and target.stat().st_size >= MAX_BYTES:
            _rotate()
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError:
        pass
