# macOS 安装路线（供 Codex Agent 执行）

仅在 Codex Desktop 宿主系统为 macOS 时执行。本路线的命令从仓库根目录运行；需要 Python 3.10+。示例应用路径为 `/Applications/ChatGPT.app`，实际路径不同时替换。

1. **安装插件。** 先确认 Python 版本，并检查 `stephen` 市场是否指向当前仓库：

   ```sh
   python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version'
   /Applications/ChatGPT.app/Contents/Resources/codex plugin marketplace list --json
   ```

   `stephen` 不在列表时执行 `/Applications/ChatGPT.app/Contents/Resources/codex plugin marketplace add .`；若指向其他目录，先修正映射。然后安装并记录命令返回的安装缓存路径：

   ```sh
   /Applications/ChatGPT.app/Contents/Resources/codex plugin add codex-token-sidebar@stephen
   ```

2. **设置 CDP 启动方式并连接 Desktop。** 检查 `curl -fsS http://127.0.0.1:9222/json/list`：其中必须存在 URL 为 `app://-/index.html`（可带查询参数）的 Desktop 主页面。若 9222 被其他应用占用，先处理端口冲突。无论当前是否已连接，都要确认用户以后从哪里启动带 CDP 的 Desktop；已配置好固定入口时无需重复创建。

   用户尚未说明启动偏好时，只问一次：「以后你想怎样启动带 CDP 的 Codex？回复 **1 一键启动器（推荐）**、**2 外部 Terminal 命令**，或直接描述你希望的其他方式。」确定选择后执行对应路线：

   - **1：** 运行 `python3 scripts/setup_macos_cdp.py app`；应用不在默认路径时加 `--app "实际的 ChatGPT.app 路径"`。生成 `~/Applications/Codex CDP.app`，请用户从 Finder 将它加入 Dock，替换原来的普通启动图标，以后从这个图标启动。
   - **2：** 运行 `python3 scripts/setup_macos_cdp.py terminal`；应用不在默认路径时同样传 `--app`。它在用户的 zsh 或 bash 启动文件中添加 `codex-cdp` 命令；请用户以后在**外部 Terminal** 运行该命令，新 Terminal 会自动加载配置。
   - **用户自行描述：** 按其启动习惯设置等效方式；必须以 `--remote-debugging-port=9222 --remote-debugging-address=127.0.0.1` 启动 Desktop，并完成下述验收。无法实现时说明原因，请用户改选 1 或 2。

   当前 Desktop 未以 CDP 方式运行时，请用户**完全退出**后按所选方式重开；集成终端会随 Desktop 退出，不能用它执行重启命令。若 Desktop 设为开机自动启动，也应改用所选入口，避免普通启动抢先占用进程。重开后复查 `/json/list` 中的 Desktop 主页面；若未出现，先核对是否从所选入口启动，再继续下一步。

3. **触发并验收 Hook。** 先运行 `plugins/codex-token-sidebar/scripts/stop_sidebar.sh --json`，避免旧实例掩盖启动失败。请用户新建 Codex 任务；若出现 `codex-token-sidebar@stephen` 的 Hook 审查提示，由用户确认信任后再新建任务。选中已有用量的任务，检查侧栏和状态：

   ```sh
   plugins/codex-token-sidebar/scripts/status_sidebar.sh --json
   python3 -c "import json,sys; from pathlib import Path; sys.path.insert(0,'plugins/codex-token-sidebar/runtime'); from identity import build_identity; print(json.dumps(build_identity(Path('plugins/codex-token-sidebar/runtime/codex_token_sidebar.py'))))"
   ```

   验收条件：状态的 `version`、`fingerprint` 与源码身份一致，`installationPath` 等于第 1 步记录的安装缓存路径；`status=running`、`health.state=healthy`、`health.reader=ok`、`health.cdp=connected`、`health.mounted=true`、`health.lastSyncAt` 有值，并且侧栏显示 Token 面板。任务刚加载时可稍后复查。

   若未启动，用 `python3 scripts/check_plugin_hooks.py --app-server /Applications/ChatGPT.app/Contents/Resources/codex --marketplace-path "$PWD/.agents/plugins/marketplace.json" --cwd "$PWD"` 只读检查 Hook；它只验证发现与信任状态。

更新、回退和卸载时再读取[维护说明](maintenance.md)。
