from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from . import config

SHORTCUT_NAME = "ModelGateway.lnk"


def _ps_quote(value: object) -> str:
    """PowerShell 单引号字符串：把内部的 ' 写成 ''。安装路径里有撇号也不怕。"""
    return "'" + str(value).replace("'", "''") + "'"


def shortcut_path() -> Path:
    # 认 APPDATA 环境变量：用户目录被重定向时 Path.home() 会指错地方
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME


def is_enabled() -> bool:
    return shortcut_path().exists()


def enable() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    main_py = config.PROJECT_ROOT / "main.py"
    lnk = str(shortcut_path())
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut({_ps_quote(lnk)});"
        f"$s.TargetPath = {_ps_quote(target)};"
        f"$s.Arguments = {_ps_quote(f'"{main_py}" --tray')};"
        f"$s.WorkingDirectory = {_ps_quote(config.PROJECT_ROOT)};"
        "$s.Description = 'Model Gateway';"
        "$s.Save()"
    )
    # -EncodedCommand（UTF-16LE + base64）：脚本经命令行传递时不再受 cmd/bash 的引号规则
    # 影响，配合 _ps_quote 把「路径里的撇号」也一起解决了
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded], capture_output=True
        )
    except OSError as exc:
        raise OSError(f"调用 PowerShell 失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "ignore").strip()[:200]
        raise OSError(f"创建启动快捷方式失败：{detail or f'powershell 退出码 {proc.returncode}'}")


def disable() -> None:
    shortcut_path().unlink(missing_ok=True)


def toggle() -> bool:
    if is_enabled():
        disable()
        return False
    enable()
    return True
