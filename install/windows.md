# Windows 原生安装路线（供 Codex Agent 执行）

仅在 Codex Desktop 宿主系统为 Windows、Agent environment 为 Windows native 时执行。命令从仓库根目录在 PowerShell 中运行；先确认 Python 3.10+：

```powershell
py -3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
```

1. **安装插件。** 使用 Desktop 随附的 CLI。整块命令在同一次 PowerShell 调用中运行；如果 Desktop 安装布局不同，先在它的 `OpenAI.Codex` 安装包内定位 `codex.exe` 并赋值给 `$codexExe`。

   ```powershell
   $desktopPackage = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1
   if (-not $desktopPackage) { throw '找不到 OpenAI.Codex 桌面应用' }
   $codexExe = Join-Path $desktopPackage.InstallLocation 'app\resources\codex.exe'
   if (-not (Test-Path $codexExe)) { throw '找不到 Desktop 内置的 codex.exe，请在应用安装包内定位' }
   & $codexExe plugin --help *> $null
   if ($LASTEXITCODE -ne 0) { throw '所选 codex.exe 不支持插件命令' }
   $repoRoot = (Resolve-Path .).Path
   $marketsJson = & $codexExe plugin marketplace list --json
   if ($LASTEXITCODE -ne 0) { throw '读取插件市场失败' }
   $marketData = ConvertFrom-Json -InputObject ($marketsJson -join [Environment]::NewLine)
   $market = $marketData.marketplaces | Where-Object { $_.name -eq 'stephen' }
   if ($market -and (Resolve-Path $market.root).Path -ne $repoRoot) { throw 'stephen 市场指向其他目录，先修复映射' }
   if (-not $market) {
     & $codexExe plugin marketplace add .
     if ($LASTEXITCODE -ne 0) { throw '注册本地市场失败' }
   }
   & $codexExe plugin add codex-token-sidebar@stephen
   if ($LASTEXITCODE -ne 0) { throw '安装插件失败' }
   ```

   记录安装命令返回的插件缓存路径。

2. **设置 CDP 启动方式并连接 Desktop。** 查询 `http://127.0.0.1:9222/json/list`，必须有符合下列条件的 Desktop 主页面。若 9222 被其他应用占用，先处理端口冲突。无论当前是否已连接，都要确认用户以后从哪里启动带 CDP 的 Desktop；已配置好固定入口时无需重复创建。

   ```powershell
   try {
     Invoke-RestMethod http://127.0.0.1:9222/json/list -TimeoutSec 2 -ErrorAction Stop | Where-Object {
       $_.type -eq 'page' -and $_.id -and $_.webSocketDebuggerUrl -and
       $_.url -match '^app://-/index\.html(?:\?|$)'
     }
   } catch { $null }
   ```

   用户尚未说明启动偏好时，只问一次：「以后你想怎样启动带 CDP 的 Codex？回复 **1 开始菜单快捷方式（推荐）**、**2 外部 PowerShell 命令**，或直接描述你希望的其他方式。」确定选择后执行对应路线：

   - **1：** 运行 `py -3 scripts/setup_windows_cdp.py app`。它创建当前用户的“Codex CDP”开始菜单快捷方式；请用户将它固定到任务栏，替换普通启动图标，以后从此入口启动。
   - **2：** 运行 `py -3 scripts/setup_windows_cdp.py terminal`。它创建 `codex-cdp.cmd` 并加入当前用户的 Path；请用户以后在**新的外部 PowerShell** 中运行 `codex-cdp`。新终端未识别命令时，重新登录 Windows 再试。
   - **用户自行描述：** 按其启动习惯设置等效方式；必须以 `--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1` 启动 Desktop，并完成下述验收。无法实现时说明原因，请用户改选 1 或 2。

   两个固定入口都会在每次启动时重新定位当前 `OpenAI.Codex` 包内的主程序。当前 Desktop 未以 CDP 方式运行时，请用户**从托盘完全退出**后按所选方式重开；集成终端会随 Desktop 退出，不能用它执行重启命令。若 Desktop 设为开机自动启动，也应改用所选入口。重开后复查 `/json/list` 中的 Desktop 主页面；若未出现，先核对是否从所选入口启动，再继续下一步。

3. **触发并验收 Hook。** 先运行 `py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py stop --json`，避免旧实例掩盖启动失败。请用户新建 Codex 任务；若出现 `codex-token-sidebar@stephen` 的 Hook 审查提示，由用户确认信任后再新建任务。选中已有用量的任务，检查侧栏和状态：

   ```powershell
   py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py status --json
   py -3 -c "import json,sys; from pathlib import Path; sys.path.insert(0,'plugins/codex-token-sidebar/runtime'); from identity import build_identity; print(json.dumps(build_identity(Path('plugins/codex-token-sidebar/runtime/codex_token_sidebar.py'))))"
   ```

   验收条件：状态的 `version`、`fingerprint` 与源码身份一致，`installationPath` 等于第 1 步记录的安装缓存路径；`status=running`、`health.state=healthy`、`health.reader=ok`、`health.cdp=connected`、`health.mounted=true`、`health.lastSyncAt` 有值，并且侧栏显示 Token 面板。任务刚加载时可稍后复查。

   若未启动，先将第 1 步确认的 CLI 绝对路径重新赋给 `$codexExe`，再运行 `py -3 scripts/check_plugin_hooks.py --app-server $codexExe --marketplace-path .agents/plugins/marketplace.json --cwd .` 只读检查 Hook；它只验证发现与信任状态。

更新、回退和卸载时再读取[维护说明](maintenance.md)。
