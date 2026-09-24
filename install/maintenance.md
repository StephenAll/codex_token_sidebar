# 安装后的维护（供 Codex 按需执行）

只在安装验收失败，或用户要求排障、更新、回退、卸载时读取本文件。首次安装从 [INSTALL.md](../INSTALL.md) 进入对应平台路线。以下命令都从包含 `.agents/plugins/marketplace.json` 的项目根目录运行；按 **Codex Desktop 的宿主系统** 选择 macOS 或 Windows，不以 WSL 内的系统信息判断。

先确认本次目标，只执行对应章节。执行后向用户说明已完成的操作、验收结果和仍未解决的问题。macOS 示例 CLI 路径为 `/Applications/ChatGPT.app/Contents/Resources/codex`，应用不在该位置时先找到实际路径；Windows 的 `$codexExe` 每次都按 [Windows 安装路线](windows.md)第 1 步重新确定。

## 排障：面板没有出现

1. 用 Desktop CLI 执行 `plugin list --json` 和 `plugin marketplace list --json`，确认 `codex-token-sidebar@stephen` 已安装，`stephen` 市场指向当前项目。若安装或映射有误，按对应平台安装路线的第 1 步修复。
2. 按 [macOS](macos.md) 或 [Windows](windows.md) 安装路线的第 2 步检查 `http://127.0.0.1:9222/json/list`：必须有 URL 为 `app://-/index.html`（可带查询参数）的 Desktop 主页面。没有时先检查是否从已配置的 CDP 入口启动；不能用其他应用占用 9222 的页面代替。
3. 请用户新建任务；如出现 Hook 信任提示，由用户处理。选中有用量的任务，读取运行状态：

   ```sh
   plugins/codex-token-sidebar/scripts/status_sidebar.sh --json
   ```

   Windows 原生 PowerShell 改用：

   ```powershell
   py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py status --json
   ```

4. 按所属平台安装路线第 3 步核对 `version`、`fingerprint`、`installationPath`、健康状态和可见面板。未启动时运行该路线提供的 `scripts/check_plugin_hooks.py` 命令检查 Hook 的发现与信任状态；**只读检查通过不等于 Hook 已在新任务中执行**。记录第一个不满足的条件，修复后再用新任务验收。

若面板正常但参考 Credits 标记为不完整，先检查是否有无法计价的响应；没有真实模型改路由时，路由卡片不会出现。这两种情况本身都不表示安装失败。

## 更新

1. 读取 `plugin list --json`、市场映射和上述运行状态，记录当前版本、指纹、安装缓存路径及源码位置。准备目标版本的完整项目目录，确认其中的市场清单和插件清单均可读取。
2. **在覆盖安装前准备回退来源。** 将当前已安装插件目录和对应版本的完整项目源码（含市场清单）备份到项目仓库之外。按安装路线第 3 步的身份检查方法，核对旧源码与已安装运行文件的版本、指纹；若实例正在运行，也要与运行状态一致。无法确认旧版来源或完成备份时，先停止更新并报告原因。
3. 通过当前版本的控制命令停止运行实例，确认返回 `status=stopped`：macOS 运行 `plugins/codex-token-sidebar/scripts/stop_sidebar.sh --json`；Windows 运行 `py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py stop --json`。macOS 从没有 `control.sock` 的早期版本迁移时，只依据已核对的完整安装路径识别旧进程；旧 PID 文件不能单独作为终止依据。
4. 若 `stephen` 市场尚未指向目标版本的项目目录，用 Desktop CLI 执行 `plugin marketplace remove stephen`，再从目标项目根目录执行 `plugin marketplace add .`；若已指向目标目录则保持不动。然后用同一个 CLI 重新安装插件；本地市场重复执行 `plugin add` 会刷新安装缓存。记录返回的安装路径：

   ```sh
   /Applications/ChatGPT.app/Contents/Resources/codex plugin add codex-token-sidebar@stephen --json
   ```

   Windows 原生 PowerShell 改用：

   ```powershell
   & $codexExe plugin add codex-token-sidebar@stephen --json
   ```

5. 请用户新建任务，按所属平台安装路线第 3 步核对新源码的版本与指纹、安装路径、`status=running`、`health.state=healthy`、`health.reader=ok`、`health.cdp=connected`、`health.mounted=true`、`health.lastSyncAt` 有值，以及侧栏中可见的面板。任务刚加载时可稍后复查。任一条件未通过，进入“回退”。

## 回退

仅在更新失败，或用户明确要求恢复旧版时执行。

1. 用对应平台的控制命令停止当前运行实例，确认 `status=stopped`。从仓库外的备份恢复旧版**项目源码**，核对插件清单、市场清单和此前记录的版本与指纹；不要直接覆盖 Codex 管理的安装缓存。
2. 若 `stephen` 市场尚未指向旧版项目目录，用 Desktop CLI 执行 `plugin marketplace remove stephen`，再从旧版项目根目录执行 `plugin marketplace add .`；不要移除其他市场。随后用上述平台的 `plugin add codex-token-sidebar@stephen --json` 重新安装，并记录安装路径。
3. 请用户新建任务；必要时按安装路线处理 Hook 信任提示。用旧版源码身份、安装路径、运行状态、健康状态和可见面板完成验收。回退仍失败时保留备份与诊断结果，向用户说明具体失败环节。

## 卸载

只在用户要求卸载时执行。先停止侧栏运行时并确认 `status=stopped`，再移除插件。macOS：

```sh
plugins/codex-token-sidebar/scripts/stop_sidebar.sh --json
/Applications/ChatGPT.app/Contents/Resources/codex plugin remove codex-token-sidebar@stephen --json
```

Windows 原生 PowerShell：

```powershell
py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py stop --json
& $codexExe plugin remove codex-token-sidebar@stephen --json
```

用 Desktop CLI 的 `plugin list --json` 确认该插件已不在已安装列表；不要顺带移除可能仍供其他插件使用的市场。

若安装时为本插件配置了固定 CDP 启动入口，再清理该入口：

- **macOS：** `~/Applications/Codex CDP.app` 仅在 `Contents/Resources/codex-token-sidebar-launcher.txt` 标记存在且确认是本项目生成的启动器时删除。终端路线仅从相应 shell 配置文件中删除 `# >>> Codex Token Sidebar CDP >>>` 到 `# <<< Codex Token Sidebar CDP <<<` 的完整区块；保留其他配置和原始 Desktop 应用。
- **Windows：** 先读取 `%LOCALAPPDATA%\Codex Token Sidebar\CDP\setup-state.json`。仅删除路径和 SHA-256 与记录一致的快捷方式、`codex-cdp.cmd` 和 `codex_cdp.py`；终端路线还要从当前用户的 Path 中移除记录的 `CDP` 目录。全部清理成功后删除状态文件；若文件已变化或无法核对，保留该文件并报告，不删除同目录中的其他内容。
