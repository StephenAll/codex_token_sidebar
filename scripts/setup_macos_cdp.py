#!/usr/bin/env python3
"""Set up a repeatable CDP launch path for Codex Desktop on macOS."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile


BUNDLE_ID = "com.openai.codex"
OPEN_COMMAND = (f"/usr/bin/open -b {BUNDLE_ID} --args "
                "--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1")
CHECK_COMMAND = ("/usr/bin/curl -fsS --max-time 1 http://127.0.0.1:9222/json/list "
                 "2>/dev/null | /usr/bin/grep -Eq 'app://-/index[.]html'")
APPLESCRIPT = f'''on run
    try
        do shell script "{OPEN_COMMAND}"
    on error errorMessage
        display dialog "无法启动 Codex Desktop：" & errorMessage buttons {{"好"}} default button "好" with icon stop
        return
    end try
    repeat 20 times
        try
            do shell script "{CHECK_COMMAND}"
            return
        end try
        delay 1
    end repeat
    display dialog "未检测到 Codex Desktop 的 CDP 页面。若 Codex Desktop 已在运行，请完全退出后再打开“Codex CDP”。" buttons {{"好"}} default button "好" with icon caution
end run
'''
START_MARKER = "# >>> Codex Token Sidebar CDP >>>"
END_MARKER = "# <<< Codex Token Sidebar CDP <<<"
TERMINAL_BLOCK = f'''{START_MARKER}
codex-cdp() {{
  {OPEN_COMMAND} || return $?
  local _codex_cdp_attempt=0
  while [ "$_codex_cdp_attempt" -lt 20 ]; do
    if {CHECK_COMMAND}; then
      return 0
    fi
    _codex_cdp_attempt=$((_codex_cdp_attempt + 1))
    /bin/sleep 1
  done
  printf '%s\\n' 'CDP 尚未连接。若 Codex Desktop 已在运行，请完全退出后重新执行 codex-cdp。' >&2
  return 1
}}
{END_MARKER}
'''
LAUNCHER_MARKER = "Contents/Resources/codex-token-sidebar-launcher.txt"


def verify_desktop(app: Path) -> None:
    info = app / "Contents/Info.plist"
    try:
        with info.open("rb") as stream:
            bundle_id = plistlib.load(stream)["CFBundleIdentifier"]
    except (OSError, KeyError, ValueError) as error:
        raise ValueError(f"找不到有效的 Codex Desktop 应用：{app}") from error
    if bundle_id != BUNDLE_ID:
        raise ValueError(f"{app} 的 Bundle ID 是 {bundle_id}，预期为 {BUNDLE_ID}")


def install_launcher(target: Path) -> Path:
    fingerprint = hashlib.sha256(APPLESCRIPT.encode("utf-8")).hexdigest() + "\n"
    marker = target / LAUNCHER_MARKER
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink() and marker.is_file() and marker.read_text(encoding="utf-8") == fingerprint:
            return target
        raise FileExistsError(f"{target} 已存在，未覆盖；请先检查它的内容")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-cdp-", dir=target.parent) as directory:
        stage = Path(directory)
        source = stage / "launcher.applescript"
        compiled = stage / target.name
        source.write_text(APPLESCRIPT, encoding="utf-8")
        subprocess.run(["/usr/bin/osacompile", "-o", str(compiled), str(source)],
                       check=True, capture_output=True, text=True)
        (compiled / LAUNCHER_MARKER).write_text(fingerprint, encoding="utf-8")
        compiled.rename(target)
    return target


def profile_for_shell(shell: str, home: Path) -> Path:
    name = Path(shell).name
    if name == "zsh":
        return home / ".zshrc"
    if name == "bash":
        return home / ".bash_profile"
    raise ValueError(f"不支持自动配置 {shell}；请让用户选择自定义启动方式")


def install_terminal_command(profile: Path) -> Path:
    content = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if TERMINAL_BLOCK in content:
        return profile
    if START_MARKER in content or END_MARKER in content:
        raise ValueError(f"{profile} 中已有不同的 CDP 配置，未覆盖")
    if re.search(r"(?m)^\s*(?:alias\s+codex-cdp=|(?:function\s+)?codex-cdp\s*\(\))", content):
        raise ValueError(f"{profile} 中已定义 codex-cdp，未覆盖")
    profile.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if content and not content.endswith("\n") else ""
    with profile.open("a", encoding="utf-8") as stream:
        stream.write(prefix + TERMINAL_BLOCK)
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("app", "terminal"))
    parser.add_argument("--app", type=Path, default=Path("/Applications/ChatGPT.app"),
                        help="Codex Desktop 应用路径")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此设置仅适用于 macOS")
    try:
        verify_desktop(args.app)
        if args.mode == "app":
            target = install_launcher(Path.home() / "Applications/Codex CDP.app")
            print(f"启动器：{target}。下次完全退出 Codex Desktop 后，从此 App 启动。")
        else:
            profile = profile_for_shell(os.environ.get("SHELL", ""), Path.home())
            install_terminal_command(profile)
            print(f"终端命令已写入 {profile}。下次完全退出 Codex Desktop 后，在新的外部 Terminal 运行 codex-cdp。")
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or str(error)).strip()
        print(f"设置 CDP 启动方式失败：{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"设置 CDP 启动方式失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
