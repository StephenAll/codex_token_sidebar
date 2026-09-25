#!/usr/bin/env python3
"""Install a desktop-visible CDP entry point, or launch Desktop through it."""

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
DESKTOP_SHORTCUT_NAME = "Codex CDP.lnk"
TERMINAL_COMMAND = ("@echo off\r\n"
                    f'py -3 "%~dp0{LAUNCHER_NAME}" run %*\r\n'
                    "exit /b %ERRORLEVEL%\r\n").encode("ascii")
PACKAGE_QUERY = (
    f"$p = Get-AppxPackage -Name '{PACKAGE_NAME}' | Sort-Object Version -Descending | "
    "Select-Object -First 1; "
    "if (-not $p) { exit 2 }; "
    "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($p.InstallLocation))"
)
DESKTOP_QUERY = (
    "$p = [Environment]::GetFolderPath('DesktopDirectory'); "
    "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($p))"
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


def _decode_payload(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise SetupError("PowerShell 没有返回路径或进程信息")
    try:
        return base64.b64decode(lines[-1], validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError) as error:
        raise SetupError("无法解码 PowerShell 的结果") from error


def decode_package_directory(output: str) -> Path:
    value = _decode_payload(output)
    if not value or not PureWindowsPath(value).is_absolute():
        raise SetupError("Desktop 目录不是绝对路径")
    return Path(value)


def package_directory() -> Path:
    result = _run_powershell(PACKAGE_QUERY)
    if result.returncode != 0:
        raise SetupError(f"找不到当前用户的 {PACKAGE_NAME} 桌面应用")
    directory = decode_package_directory(result.stdout)
    if not directory.is_dir():
        raise SetupError(f"Desktop 安装目录不可访问：{directory}")
    return directory


def desktop_directory() -> Path:
    result = _run_powershell(DESKTOP_QUERY)
    if result.returncode != 0:
        raise SetupError("无法定位当前用户的桌面目录")
    directory = decode_package_directory(result.stdout)
    if not directory.is_dir():
        raise SetupError(f"桌面目录不可访问：{directory}")
    return directory


def support_directory() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    if not local_data or not Path(local_data).is_absolute():
        raise SetupError("无法定位当前用户的 LocalAppData 目录")
    return Path(local_data) / "Codex Token Sidebar" / "CDP"


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


def main_processes(executable: Path) -> list[dict]:
    environment = dict(os.environ, CODEX_CDP_EXE=str(executable))
    command = (
        "$name = [IO.Path]::GetFileName($env:CODEX_CDP_EXE); "
        "$rows = @(Get-CimInstance Win32_Process -Filter (\"Name='\" + $name + \"'\") -ErrorAction Stop | "
        "Where-Object { $_.ExecutablePath -and $_.ExecutablePath -ieq $env:CODEX_CDP_EXE "
        "-and $_.CommandLine -notmatch ' --type=' } | Select-Object ProcessId, CommandLine); "
        "$json = ConvertTo-Json -InputObject $rows -Compress; "
        "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($json))"
    )
    result = _run_powershell(command, environment=environment)
    if result.returncode != 0:
        raise SetupError("无法检查 Desktop 主进程")
    try:
        value = json.loads(_decode_payload(result.stdout))
    except ValueError as error:
        raise SetupError("Desktop 主进程信息无效") from error
    if value is None:
        return []
    rows = value if isinstance(value, list) else [value]
    if not all(isinstance(row, dict) for row in rows):
        raise SetupError("Desktop 主进程信息无效")
    return rows


def _has_cdp_flags(process: dict) -> bool:
    command = process.get("CommandLine")
    return isinstance(command, str) and all(flag in command for flag in CDP_FLAGS)


def _log_launch(event: str) -> None:
    # The external installer copies the runner into its durable support directory.
    if Path(__file__).name != LAUNCHER_NAME:
        return
    log = Path(__file__).resolve().parent / "launch.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event}\n")
    except OSError:
        pass


def launch_desktop() -> None:
    executable = desktop_executable(package_directory())
    running = main_processes(executable)
    if running:
        if has_desktop_page() and any(_has_cdp_flags(process) for process in running):
            return
        try:
            input("请从托盘完全退出 Desktop，然后按 Enter 继续：")
        except EOFError as error:
            raise SetupError("请在外部命令窗口运行桌面启动器") from error
        if main_processes(executable):
            raise SetupError("Desktop 仍在运行；请从托盘完全退出后重试")
    _log_launch("starting")
    subprocess.Popen([str(executable), *CDP_FLAGS], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if has_desktop_page() and any(_has_cdp_flags(process)
                                      for process in main_processes(executable)):
            _log_launch("connected")
            return
        time.sleep(0.5)
    raise SetupError("CDP 主页面或 Desktop 主进程参数未通过验证；请检查 launch.log")


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


def _create_shortcut(path: Path, launcher: Path) -> None:
    python = shutil.which("py.exe") or shutil.which("py")
    if not python:
        raise SetupError("找不到 py 启动器；请先确认外部 PowerShell 可运行 py -3")
    environment = dict(os.environ, CODEX_CDP_LINK=str(path), CODEX_CDP_PY=python,
                       CODEX_CDP_SCRIPT=str(launcher))
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shortcut = $shell.CreateShortcut($env:CODEX_CDP_LINK); "
        "$shortcut.TargetPath = $env:CODEX_CDP_PY; "
        "$shortcut.Arguments = '-3 \"' + $env:CODEX_CDP_SCRIPT + '\" run --pause-on-error'; "
        "$shortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($env:CODEX_CDP_SCRIPT); "
        "$shortcut.Description = 'Start Codex Desktop with CDP'; "
        "$shortcut.Save()"
    )
    result = _run_powershell(command, environment=environment)
    if result.returncode != 0 or not path.is_file():
        raise SetupError("无法在桌面创建 CDP 快捷方式")


def _write_shortcut_owned(path: Path, launcher: Path, state: dict) -> None:
    if path.is_symlink():
        raise SetupError(f"拒绝覆盖链接：{path}")
    if path.exists() and (state.get("desktop_shortcut_path") != str(path)
                          or state.get("desktop_shortcut_sha256") != _digest(path.read_bytes())):
        raise SetupError(f"已有非本工具管理的快捷方式，未覆盖：{path}")
    with tempfile.NamedTemporaryFile(prefix=".codex-cdp-", suffix=".lnk",
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    temporary.unlink()
    try:
        _create_shortcut(temporary, launcher)
        digest = _digest(temporary.read_bytes())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    state["desktop_shortcut_path"] = str(path)
    state["desktop_shortcut_sha256"] = digest


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
    directory = support_directory()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "setup-state.json"
    state = _load_state(state_path)
    source = Path(__file__).resolve().read_bytes()
    launcher = directory / LAUNCHER_NAME

    if mode == "app":
        output = desktop_directory() / DESKTOP_SHORTCUT_NAME
        _write_owned(launcher, source, state, "launcher_sha256")
        _write_shortcut_owned(output, launcher, state)
    else:
        _write_owned(launcher, source, state, "launcher_sha256")
        _write_owned(directory / COMMAND_NAME, TERMINAL_COMMAND, state, "command_sha256")
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
    parser.add_argument("--pause-on-error", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("此设置仅适用于 Windows 原生环境")
    try:
        if args.mode == "run":
            launch_desktop()
            print("Desktop CDP 已连接")
        else:
            target = install(args.mode)
            print(f"启动入口已配置：{target}。下次完全退出 Desktop 后，从此入口启动。")
    except (OSError, ValueError, SetupError, subprocess.TimeoutExpired) as error:
        message = f"设置或启动 Desktop CDP 失败：{error}"
        _log_launch(f"failed {type(error).__name__}: {str(error)[:512]}")
        print(message, file=sys.stderr)
        if args.pause_on_error:
            try:
                input("按 Enter 关闭窗口…")
            except EOFError:
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
