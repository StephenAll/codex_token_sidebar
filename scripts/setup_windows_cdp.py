#!/usr/bin/env python3
"""Install or run a repeatable CDP entry point for ChatGPT Desktop on Windows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


PACKAGE_NAME = "OpenAI.Codex"
CDP_FLAGS = ("--remote-debugging-port=9222", "--remote-debugging-address=127.0.0.1")
LAUNCHER_NAME = "codex_cdp.py"
COMMAND_NAME = "codex-cdp.cmd"
SHORTCUT_NAME = "Codex CDP.lnk"
COMMAND = ("@echo off\r\n"
           "rem Codex Token Sidebar CDP command\r\n"
           f'py -3 "%~dp0{LAUNCHER_NAME}" run %*\r\n'
           "exit /b %ERRORLEVEL%\r\n").encode("utf-8")
PACKAGE_QUERY = (
    f"$p = Get-AppxPackage -Name '{PACKAGE_NAME}' | Sort-Object Version -Descending | "
    "Select-Object -First 1; "
    "if (-not $p) { exit 2 }; "
    "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($p.InstallLocation))"
)


class SetupError(RuntimeError):
    pass


def _powershell() -> str:
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidate = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe") or "powershell.exe"


def _run_powershell(command: str, *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", command],
                          env=environment, capture_output=True, text=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def decode_package_directory(output: str) -> Path:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise SetupError(f"未找到 {PACKAGE_NAME} 的安装目录")
    try:
        value = base64.b64decode(lines[-1], validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError) as error:
        raise SetupError("无法读取 Desktop 安装目录") from error
    if not value or not PureWindowsPath(value).is_absolute():
        raise SetupError("Desktop 安装目录不是绝对路径")
    return Path(value)


def package_directory() -> Path:
    result = _run_powershell(PACKAGE_QUERY)
    if result.returncode != 0:
        raise SetupError(f"找不到当前用户的 {PACKAGE_NAME} 桌面应用")
    directory = decode_package_directory(result.stdout)
    if not directory.is_dir():
        raise SetupError(f"Desktop 安装目录不可访问：{directory}")
    return directory


def desktop_executable(package: Path) -> Path:
    manifest = package / "AppxManifest.xml"
    if manifest.is_file():
        try:
            root = ET.parse(manifest).getroot()
        except ET.ParseError as error:
            raise SetupError("Desktop 的 AppxManifest.xml 无法解析") from error
        applications = [element for element in root.iter()
                        if element.tag.rsplit("}", 1)[-1] == "Application"
                        and element.get("Executable")]
        primary = [element for element in applications if element.get("Id") == "App"]
        chosen = primary if primary else applications
        if len(chosen) != 1:
            raise SetupError("无法唯一确定 Desktop 的主程序")
        relative = PureWindowsPath(chosen[0].get("Executable"))
        if (relative.is_absolute() or relative.drive or ".." in relative.parts
                or relative.suffix.lower() != ".exe"):
            raise SetupError("Desktop 主程序路径无效")
        executable = package.joinpath(*relative.parts)
        if not executable.resolve().is_relative_to(package.resolve()) or not executable.is_file():
            raise SetupError(f"Desktop 主程序不可访问：{executable}")
        return executable

    candidates = [package / "app" / name for name in ("ChatGPT.exe", "Codex.exe")]
    available = [candidate for candidate in candidates if candidate.is_file()]
    if len(available) != 1:
        raise SetupError("找不到唯一的 Desktop 主程序；请检查 MSIX 安装")
    return available[0]


def is_desktop_page(targets: object) -> bool:
    return isinstance(targets, list) and any(
        isinstance(target, dict) and target.get("type") == "page" and target.get("id")
        and target.get("webSocketDebuggerUrl")
        and isinstance(target.get("url"), str)
        and re.match(r"^app://-/index\.html(?:\?|$)", target["url"])
        for target in targets
    )


def has_desktop_page() -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", 9222, timeout=0.8)
    try:
        connection.request("GET", "/json/list")
        response = connection.getresponse()
        return response.status == 200 and is_desktop_page(json.loads(response.read(1024 * 1024)))
    except (OSError, ValueError, TimeoutError):
        return False
    finally:
        connection.close()


def launch_desktop() -> None:
    executable = desktop_executable(package_directory())
    environment = dict(os.environ, CODEX_CDP_EXE=str(executable))
    command = ("Start-Process -FilePath $env:CODEX_CDP_EXE "
               f"-ArgumentList @('{CDP_FLAGS[0]}','{CDP_FLAGS[1]}') "
               "-ErrorAction Stop")
    result = _run_powershell(command, environment=environment)
    if result.returncode != 0:
        raise SetupError(f"无法启动 Desktop：{result.stderr.strip() or result.stdout.strip()}")
    for _ in range(40):
        if has_desktop_page():
            return
        time.sleep(0.5)
    raise SetupError("CDP 尚未连接。若 Desktop 已在普通模式运行，请从托盘完全退出后重新启动 Codex CDP。")


def state_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SetupError("找不到 LOCALAPPDATA")
    return Path(local) / "Codex Token Sidebar" / "CDP"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SetupError(f"安装状态文件无法读取：{path}") from error
    if not isinstance(value, dict):
        raise SetupError(f"安装状态文件无效：{path}")
    return value


def _write_owned(path: Path, content: bytes, state: dict, key: str) -> None:
    if path.is_symlink():
        raise SetupError(f"拒绝覆盖链接：{path}")
    if path.exists():
        existing = _digest(path.read_bytes())
        if existing != _digest(content) and state.get(key) != existing:
            raise SetupError(f"已有非本工具管理的文件，未覆盖：{path}")
        if existing == _digest(content):
            state[key] = existing
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".codex-cdp-", dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    state[key] = _digest(content)


def _save_state(path: Path, state: dict) -> None:
    content = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.is_symlink():
        raise SetupError(f"拒绝覆盖链接：{path}")
    with tempfile.NamedTemporaryFile(prefix=".codex-cdp-state-", dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def windowless_python() -> tuple[Path, str]:
    launcher = shutil.which("pyw.exe")
    if launcher:
        return Path(launcher), "-3 "
    executable = Path(sys.executable).with_name("pythonw.exe")
    if executable.is_file():
        return executable, ""
    raise SetupError("找不到 pyw.exe 或 pythonw.exe；请改用终端命令")


def _create_shortcut(link: Path, python: Path, prefix: str, launcher: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-cdp-shortcut-", dir=link.parent) as directory:
        staged = Path(directory) / SHORTCUT_NAME
        environment = dict(os.environ)
        environment.update({
            "CODEX_CDP_LINK": str(staged),
            "CODEX_CDP_PYTHON": str(python),
            "CODEX_CDP_ARGUMENTS": f'{prefix}"{launcher}" run --gui',
            "CODEX_CDP_HOME": str(launcher.parent),
        })
        command = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CODEX_CDP_LINK); "
            "$s.TargetPath = $env:CODEX_CDP_PYTHON; "
            "$s.Arguments = $env:CODEX_CDP_ARGUMENTS; "
            "$s.WorkingDirectory = $env:CODEX_CDP_HOME; "
            "$s.Description = 'Start ChatGPT with CDP for Codex Token Sidebar'; "
            "$s.Save()"
        )
        result = _run_powershell(command, environment=environment)
        if result.returncode != 0 or not staged.is_file():
            raise SetupError(f"无法创建开始菜单快捷方式：{result.stderr.strip() or result.stdout.strip()}")
        staged.replace(link)


def path_with_entry(existing: str, entry: Path) -> str:
    normalized = ntpath.normcase(ntpath.normpath(str(entry)))
    for item in existing.split(";"):
        if item and ntpath.normcase(ntpath.normpath(os.path.expandvars(item.strip('"')))) == normalized:
            return existing
    return existing + ("" if not existing or existing.endswith(";") else ";") + str(entry)


def _broadcast_environment() -> bool:
    import ctypes
    from ctypes import wintypes

    result = ctypes.c_size_t()
    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                     wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
                     ctypes.POINTER(ctypes.c_size_t))
    send.restype = wintypes.LPARAM
    return bool(send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000,
                     ctypes.byref(result)))


def add_user_path(entry: Path) -> bool:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        if not isinstance(current, str):
            raise SetupError("用户 Path 注册表值不是字符串")
        updated = path_with_entry(current, entry)
        if updated == current:
            return True
        winreg.SetValueEx(key, "Path", 0, kind, updated)
    return _broadcast_environment()


def install(mode: str) -> Path:
    desktop_executable(package_directory())
    directory = state_directory()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "setup-state.json"
    state = _load_state(state_path)
    source = Path(__file__).resolve().read_bytes()
    launcher = directory / LAUNCHER_NAME

    if mode == "app":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise SetupError("找不到 APPDATA")
        link = Path(appdata) / "Microsoft/Windows/Start Menu/Programs" / SHORTCUT_NAME
        if link.exists():
            if (state.get("shortcut_path") != str(link)
                    or state.get("shortcut_sha256") != _digest(link.read_bytes())):
                raise SetupError(f"开始菜单已有非本工具管理的快捷方式，未覆盖：{link}")
        python, prefix = windowless_python()
        _write_owned(launcher, source, state, "launcher_sha256")
        _create_shortcut(link, python, prefix, launcher)
        state["shortcut_path"] = str(link)
        state["shortcut_sha256"] = _digest(link.read_bytes())
        output = link
    else:
        _write_owned(launcher, source, state, "launcher_sha256")
        _write_owned(directory / COMMAND_NAME, COMMAND, state, "command_sha256")
        path_updated = add_user_path(directory)
        state["terminal_path"] = str(directory)
        if not path_updated:
            print("用户 Path 已更新；若新终端尚未识别 codex-cdp，请重新登录 Windows。", file=sys.stderr)
        output = directory / COMMAND_NAME
    _save_state(state_path, state)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("app", "terminal", "run"))
    parser.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("此设置仅适用于 Windows 原生环境")
    if args.gui and args.mode != "run":
        parser.error("--gui 仅适用于 run")
    try:
        if args.mode == "run":
            launch_desktop()
            if not args.gui:
                print("Desktop CDP 已连接")
        else:
            target = install(args.mode)
            print(f"启动入口已配置：{target}。下次完全退出 Desktop 后，从此入口启动。")
    except (OSError, ValueError, SetupError, subprocess.TimeoutExpired) as error:
        message = f"设置或启动 Desktop CDP 失败：{error}"
        if args.gui:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Codex CDP", 0x10)
        else:
            print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
