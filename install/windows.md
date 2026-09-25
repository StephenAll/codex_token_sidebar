# Windows 原生安装路线（供 Codex Agent 执行）

仅在 Codex Desktop 宿主系统为 Windows、Agent environment 为 Windows native 时执行。命令从仓库根目录在 PowerShell 中运行；先确认 Python 3.10+：

```powershell
py -3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
```

若失败，先让用户在正常 Windows 环境中安装 Python 3.10+，重启 Desktop，并确认 Agent 和外部 PowerShell 都能执行 `py -3`；Hook 也依赖这个命令。

1. **安装插件。** 依次检查显式配置的 `CODEX_CLI_PATH`、Desktop 包内 CLI 和正在运行的 app-server CLI，选用当前 Agent 可执行的文件。整块命令在同一次 PowerShell 调用中运行，不写死缓存路径。

   ```powershell
   $desktopPackage = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1
   if (-not $desktopPackage) { throw '找不到 OpenAI.Codex 桌面应用' }
   $bundledCli = Join-Path $desktopPackage.InstallLocation 'app\resources\codex.exe'
   try {
     $runningCli = @(Get-CimInstance Win32_Process -Filter "Name='codex.exe'" -ErrorAction Stop |
       Where-Object { $_.ExecutablePath -and $_.CommandLine -match 'app-server' } |
       Select-Object -ExpandProperty ExecutablePath)
   } catch { $runningCli = @() }
   $codexExe = $null
   $candidates = @($env:CODEX_CLI_PATH, $bundledCli) + $runningCli
   foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
     try {
       if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
       & $candidate plugin --help *> $null
       if ($? -and $LASTEXITCODE -eq 0) { $codexExe = $candidate; break }
     } catch {}
   }
   if (-not $codexExe) { throw '找不到可执行插件命令的 Desktop CLI；检查当前 app-server 进程的可执行路径' }
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

   用户尚未说明启动偏好时，只问一次：「以后你想怎样启动带 CDP 的 Codex？回复 **1 桌面启动器（推荐）**、**2 外部 PowerShell 命令**，或直接描述你希望的其他方式。」确定选择后执行对应路线。

   **1 或 2 都由用户在资源管理器打开的外部 PowerShell 中完成设置**，确保启动入口可供桌面进程使用。Agent 提供当前仓库的绝对路径，用户运行以下命令，并把 `app` 替换为所选路线的 `terminal`（选择 2 时）：

   ```powershell
   $repo = '<Agent 提供的仓库根目录绝对路径>'
   $setup = Join-Path $repo 'scripts\setup_windows_cdp.py'
   if (-not (Test-Path -LiteralPath $setup)) { throw '外部 PowerShell 看不到仓库；先将仓库克隆到普通用户可见目录' }
   py -3 $setup app
   ```

   - **1：** 外部设置命令只在桌面创建 `Codex CDP.lnk`。启动脚本、状态文件和日志留在 `%LOCALAPPDATA%\Codex Token Sidebar\CDP`；请用户以后双击快捷方式。若桌面已有同名快捷方式且不属于本工具，先检查并重命名旧快捷方式，再重新运行设置命令。
   - **2：** 外部设置命令在同一 `%LOCALAPPDATA%\Codex Token Sidebar\CDP` 目录创建 `codex-cdp.cmd` 并将该目录加入当前用户的 Path；请用户以后在新的外部 PowerShell 中运行 `codex-cdp`。新终端未识别命令时重新登录 Windows。
   - **用户自行描述：** 按其启动习惯设置等效方式；必须以 `--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1` 启动 Desktop，并完成下述验收。无法实现时说明原因，请用户改选 1 或 2。

   两个入口都会在每次启动时重新定位当前 `OpenAI.Codex` 包内的主程序；若普通实例正在运行，命令窗口会要求用户先从托盘完全退出。脚本用主进程的 CDP 参数和 `/json/list` 主页面共同验收。若 Desktop 设为开机自动启动，应改用所选入口。重开后 Agent 再复查 `/json/list`，确认从所选入口启动，才继续下一步。

3. **触发并验收 Hook。** 先运行 `py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py stop --json`，避免旧实例掩盖启动失败。请用户新建 Codex 任务；若出现 `codex-token-sidebar@stephen` 的 Hook 审查提示，由用户确认信任后再新建任务。选中已有用量的任务，检查侧栏和状态：

   ```powershell
   py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py status --json
   py -3 -c "import json,sys; from pathlib import Path; sys.path.insert(0,'plugins/codex-token-sidebar/runtime'); from identity import build_identity; print(json.dumps(build_identity(Path('plugins/codex-token-sidebar/runtime/codex_token_sidebar.py'))))"
   ```

   验收条件：状态的 `version`、`fingerprint` 与源码身份一致，`installationPath` 等于第 1 步记录的安装缓存路径；`status=running`、`health.state=healthy`、`health.reader=ok`、`health.cdp=connected`、`health.mounted=true`、`health.lastSyncAt` 有值，并且侧栏显示 Token 面板。任务刚加载时可稍后复查。

   若未启动，先将第 1 步确认的 CLI 绝对路径重新赋给 `$codexExe`，再运行 `py -3 scripts/check_plugin_hooks.py --app-server $codexExe --marketplace-path .agents/plugins/marketplace.json --cwd .` 只读检查 Hook；它只验证发现与信任状态。

更新、回退和卸载时再读取[维护说明](maintenance.md)。
