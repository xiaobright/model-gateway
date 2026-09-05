from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import config

SHORTCUT_NAME = "ModelGateway.lnk"


def shortcut_path() -> Path:
    appdata = Path.home() / "AppData" / "Roaming"
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME


def is_enabled() -> bool:
    return shortcut_path().exists()


def enable() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    main_py = config.PROJECT_ROOT / "main.py"
    lnk = str(shortcut_path())
    script = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$s = $ws.CreateShortcut('{lnk}');"
        f"$s.TargetPath = '{target}';"
        f"$s.Arguments = '\"{main_py}\" --tray';"
        f"$s.WorkingDirectory = '{config.PROJECT_ROOT}';"
        "$s.Description = 'Model Gateway';"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True)


def disable() -> None:
    shortcut_path().unlink(missing_ok=True)


def toggle() -> bool:
    if is_enabled():
        disable()
        return False
    enable()
    return True
