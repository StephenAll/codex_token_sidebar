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
DESKTOP_LAUNCHER_NAME = "codex_cdp_desktop.pyw"
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


def pythonw_executable() -> Path:
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        raise SetupError(f"找不到与当前 Python 对应的无控制台启动器：{executable}")
    return executable


def _create_shortcut(path: Path, launcher: Path, desktop_exe: Path) -> None:
    environment = dict(os.environ, CODEX_CDP_LINK=str(path), CODEX_CDP_PY=str(pythonw_executable()),
                       CODEX_CDP_SCRIPT=str(launcher), CODEX_CDP_ICON=f"{desktop_exe},0")
    command = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shortcut = $shell.CreateShortcut($env:CODEX_CDP_LINK); "
        "$shortcut.TargetPath = $env:CODEX_CDP_PY; "
        "$shortcut.Arguments = '-X utf8 \"' + $env:CODEX_CDP_SCRIPT + '\"'; "
        "$shortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($env:CODEX_CDP_SCRIPT); "
        "$shortcut.IconLocation = $env:CODEX_CDP_ICON; "
        "$shortcut.Description = 'Start Codex Desktop with CDP'; "
        "$shortcut.Save()"
    )
    result = _run_powershell(command, environment=environment)
    if result.returncode != 0 or not path.is_file():
        raise SetupError("无法在桌面创建 CDP 快捷方式")


def _write_shortcut_owned(path: Path, launcher: Path, desktop_exe: Path, state: dict) -> None:
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
        _create_shortcut(temporary, launcher, desktop_exe)
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
    desktop_exe = desktop_executable(package_directory())
    directory = support_directory()
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "setup-state.json"
    state = _load_state(state_path)
    source = Path(__file__).resolve().read_bytes()
    launcher = directory / LAUNCHER_NAME

    if mode == "app":
        desktop_source = Path(__file__).with_name(DESKTOP_LAUNCHER_NAME)
        if not desktop_source.is_file():
            raise SetupError(f"缺少桌面启动脚本：{desktop_source}")
        desktop_launcher = directory / DESKTOP_LAUNCHER_NAME
        output = desktop_directory() / DESKTOP_SHORTCUT_NAME
        _write_owned(launcher, source, state, "launcher_sha256")
        _write_owned(desktop_launcher, desktop_source.read_bytes(), state, "desktop_launcher_sha256")
        _write_shortcut_owned(output, desktop_launcher, desktop_exe, state)
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


def remove_user_path(entry: Path) -> None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                             winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return
    with key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return
        if not isinstance(current, str):
            raise SetupError("用户 Path 注册表值不是字符串")
        normalized = ntpath.normcase(ntpath.normpath(str(entry)))
        remaining = [part for part in current.split(";")
                     if ntpath.normcase(ntpath.normpath(os.path.expandvars(part.strip('"'))))
                     != normalized]
        updated = ";".join(remaining)
        if updated != current:
            winreg.SetValueEx(key, "Path", 0, kind, updated)
    _broadcast_environment()


def uninstall() -> list[Path]:
    """Remove recorded CDP entries from the same external environment as installation."""
    directory = support_directory()
    runtime_directory = directory.parent
    state_path = directory / "setup-state.json"
    current_shortcut = desktop_directory() / DESKTOP_SHORTCUT_NAME
    if runtime_directory.is_symlink() or directory.is_symlink() or state_path.is_symlink():
        raise SetupError("插件运行目录或安装状态文件是链接，未执行清理")
    if not state_path.exists():
        if current_shortcut.exists() or current_shortcut.is_symlink() or directory.exists():
            raise SetupError("缺少 CDP 安装记录，无法确认现有文件归属；未清理，请交给 Agent 核对")
    state = _load_state(state_path) if state_path.exists() else {}
    if state_path.exists() and not state.get("launcher_sha256"):
        raise SetupError("CDP 安装记录缺少启动脚本指纹，未执行清理")
    candidates = {current_shortcut: state.get("desktop_shortcut_sha256")}
    recorded = state.get("desktop_shortcut_path")
    if recorded is not None:
        if not isinstance(recorded, str):
            raise SetupError("记录的桌面快捷方式路径无效")
        shortcut = Path(recorded)
        if not shortcut.is_absolute() or shortcut.name != DESKTOP_SHORTCUT_NAME:
            raise SetupError("记录的桌面快捷方式路径无效")
        candidates[shortcut] = state.get("desktop_shortcut_sha256")
    for name, key in ((LAUNCHER_NAME, "launcher_sha256"),
                      (DESKTOP_LAUNCHER_NAME, "desktop_launcher_sha256"),
                      (COMMAND_NAME, "command_sha256")):
        candidates[directory / name] = state.get(key)

    # Validate every owned file before deleting anything, retaining evidence on failure.
    owned = []
    for path, digest in candidates.items():
        if path.is_symlink():
            raise SetupError(f"文件是链接，未清理：{path}")
        if path.exists():
            if not path.is_file() or not digest or _digest(path.read_bytes()) != digest:
                raise SetupError(f"文件与安装记录不符，未清理：{path}")
            owned.append(path)
    terminal_path = state.get("terminal_path")
    if terminal_path is not None and terminal_path != str(directory):
        raise SetupError("记录的终端 Path 与安装目录不符，未清理")
    logs = [directory / name for name in ("launch.log", "desktop-launch.log")]
    for path in logs:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SetupError(f"日志路径异常，未清理：{path}")
    cache = directory / "__pycache__"
    if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
        raise SetupError(f"缓存路径异常，未清理：{cache}")
    bytecode = list(cache.glob("codex_cdp.cpython-*.pyc")) if cache.exists() else []
    for path in bytecode:
        if path.is_symlink() or not path.is_file():
            raise SetupError(f"缓存文件异常，未清理：{path}")
    runtime_files = [runtime_directory / name for name in
                     ("control.json", "instance.lock", "launch.lock", "sidebar.log",
                      "credits-rates.json")]
    for path in runtime_files:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise SetupError(f"运行状态路径异常，未清理：{path}")
    if terminal_path:
        remove_user_path(directory)
    for path in owned:
        path.unlink()
    for path in logs:
        path.unlink(missing_ok=True)
    for path in bytecode:
        path.unlink()
    if cache.exists() and not any(cache.iterdir()):
        cache.rmdir()
    state_path.unlink(missing_ok=True)
    remaining = list(directory.iterdir()) if directory.exists() else []
    if directory.exists() and not remaining:
        directory.rmdir()
    for path in runtime_files:
        path.unlink(missing_ok=True)
    if runtime_directory.exists():
        remaining.extend(path for path in runtime_directory.iterdir() if path != directory)
    if runtime_directory.exists() and not remaining:
        runtime_directory.rmdir()
    return remaining


def verify_desktop_entry(shortcut: Path) -> None:
    """Check the installed files from the process that will expose them to Desktop."""
    support = support_directory()
    runner = support / DESKTOP_LAUNCHER_NAME
    if not runner.is_file() or not (support / LAUNCHER_NAME).is_file():
        raise SetupError("桌面进程可见的 CDP 启动文件不完整")
    diagnosis = subprocess.run([sys.executable, "-X", "utf8", str(runner), "--diagnose"],
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if diagnosis.returncode != 0:
        raise SetupError(f"桌面进程无法运行 CDP 启动文件：{diagnosis.stderr.strip()[-500:]}")
    try:
        report = json.loads(diagnosis.stdout)
        if not isinstance(report, dict) or not report.get("executable"):
            raise ValueError("invalid diagnosis")
    except ValueError as error:
        raise SetupError("CDP 启动文件的预检结果无效") from error

    environment = dict(os.environ, CODEX_CDP_LINK=str(shortcut))
    command = (
        "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CODEX_CDP_LINK); "
        "$json = @{target=$shortcut.TargetPath; args=$shortcut.Arguments; "
        "work=$shortcut.WorkingDirectory} | ConvertTo-Json -Compress; "
        "[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($json))"
    )
    result = _run_powershell(command, environment=environment)
    if result.returncode != 0:
        raise SetupError("无法读取桌面 CDP 快捷方式")
    try:
        link = json.loads(_decode_payload(result.stdout))
        if (not isinstance(link, dict)
                or not all(isinstance(link.get(key), str) for key in ("target", "args", "work"))):
            raise ValueError("invalid shortcut")
    except ValueError as error:
        raise SetupError("桌面 CDP 快捷方式信息无效") from error
    expected_args = f'-X utf8 "{runner}"'
    if (ntpath.normcase(link.get("target", "")) != ntpath.normcase(str(pythonw_executable()))
            or link.get("args") != expected_args
            or ntpath.normcase(link.get("work", "")) != ntpath.normcase(str(support))):
        raise SetupError("桌面 CDP 快捷方式的目标、参数或起始目录不正确")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("app", "terminal", "run", "uninstall"))
    parser.add_argument("--pause-on-error", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("此设置仅适用于 Windows 原生环境")
    try:
        if args.mode == "uninstall":
            had_install_record = (support_directory() / "setup-state.json").is_file()
            remaining = uninstall()
            message = ("已清理当前 Agent 可见的 CDP 安装文件；请核对桌面快捷方式。"
                       if had_install_record else
                       "当前 Agent 环境没有 CDP 安装记录；无法据此确认桌面入口已移除。")
            if remaining:
                message += " 保留未登记内容：" + ", ".join(map(str, remaining))
        elif args.mode == "run":
            launch_desktop()
            message = "Desktop CDP 已连接"
        else:
            target = install(args.mode)
            if args.mode == "app":
                verify_desktop_entry(target)
            message = f"启动入口已配置：{target}。下次完全退出 Desktop 后，从此入口启动。"
    except (OSError, ValueError, SetupError, subprocess.TimeoutExpired) as error:
        action = "CDP 入口清理失败" if args.mode == "uninstall" else "设置或启动 Desktop CDP 失败"
        message = f"{action}：{error}"
        _log_launch(f"failed {type(error).__name__}: {str(error)[:512]}")
        print(message, file=sys.stderr)
        if args.pause_on_error:
            try:
                input("按 Enter 关闭窗口…")
            except EOFError:
                pass
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
