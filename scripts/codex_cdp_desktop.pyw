"""Launch Codex Desktop with CDP without a console window."""

from __future__ import annotations

import ctypes
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parent
LOG = ROOT / "desktop-launch.log"


def log(message: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + message + "\n")
    except OSError:
        pass


def load_core():
    script = ROOT / "codex_cdp.py"
    spec = importlib.util.spec_from_file_location("cdp_setup", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 CDP 启动脚本：{script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(*, diagnose: bool = False) -> None:
    log("launcher entered")
    setup = load_core()
    executable = setup.desktop_executable(setup.package_directory())
    running = setup.main_processes(executable)
    connected = setup.has_desktop_page()
    report = {
        "executable": str(executable),
        "runningPids": [process.get("ProcessId") for process in running],
        "cdpConnected": connected,
        "cdpFlagsPresent": any(setup._has_cdp_flags(process) for process in running),
    }
    log(json.dumps(report, ensure_ascii=False))
    if diagnose:
        print(json.dumps(report, ensure_ascii=False))
        return
    if running:
        if connected and report["cdpFlagsPresent"]:
            log("already connected")
            return
        raise RuntimeError("Codex Desktop 仍在后台运行。请从系统托盘完全退出后再启动。")

    process = subprocess.Popen([str(executable), *setup.CDP_FLAGS],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    log(f"started Desktop pid={process.pid}")
    for _ in range(40):
        if (setup.has_desktop_page()
                and any(setup._has_cdp_flags(item)
                        for item in setup.main_processes(executable))):
            log("VERIFIED: CDP main page and process flags")
            return
        time.sleep(0.5)
    raise RuntimeError(f"Codex 已启动，但 CDP 连接未通过验证。详细记录在：{LOG}")


if __name__ == "__main__":
    diagnosis = "--diagnose" in sys.argv
    try:
        main(diagnose=diagnosis)
    except Exception as error:
        log(traceback.format_exc())
        if diagnosis:
            raise
        ctypes.windll.user32.MessageBoxW(
            None, f"{error}\n\n日志：{LOG}", "Codex CDP 启动失败", 0x10)
        raise SystemExit(1) from None
