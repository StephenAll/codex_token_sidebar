# 安装后的维护（供 Codex Agent 按需读取）

所属平台路线验收失败，或用户要求更新、回退、卸载时再读取本文件；正常安装使用对应的 [macOS](macos.md) 或 [Windows](windows.md) 路线。

## 排障

没有面板时，按所属平台路线依次核对 `/json/list` 中的 Desktop 主页面、Hook 的发现和信任状态、运行实例身份及健康状态。只读 Hook 检查不能证明它已在新任务中执行。参考 Credits 与模型改路由的功能说明见仓库 [README](../README.md)。

## 更新与回退

更新前记录当前安装版本并保存可恢复的插件副本，再安装新版本；新建任务后重新核对版本、指纹、健康状态和可见面板。macOS 上从没有 `control.sock` 的早期版本迁移时，应根据完整安装路径识别并停止旧运行时；旧 PID 文件不能作为终止进程的依据。

更新失败时先通过新运行时的控制接口停止实例，再从备份恢复旧版插件。必要时重新审查 Hook，并在新任务中复查面板与健康状态。备份与回退记录保存在仓库之外。

## 卸载

先停止侧栏运行时，再移除插件。macOS：

```sh
plugins/codex-token-sidebar/scripts/stop_sidebar.sh
/Applications/ChatGPT.app/Contents/Resources/codex plugin remove codex-token-sidebar@stephen
```

若 macOS 安装时配置过固定 CDP 入口，卸载时也清理该入口：启动器位于 `~/Applications/Codex CDP.app`，仅删除带 `Contents/Resources/codex-token-sidebar-launcher.txt` 标记的 App；终端路线只删除 shell 配置中 `# >>> Codex Token Sidebar CDP >>>` 至 `# <<< Codex Token Sidebar CDP <<<` 的区块。不要改动原始 Desktop 应用。

Windows 原生 PowerShell：

```powershell
py -3 plugins/codex-token-sidebar/runtime/windows_lifecycle.py stop --json
& $codexExe plugin remove codex-token-sidebar@stephen
```

Windows 的 `$codexExe` 取 [Windows 安装路线](windows.md)中确认的 Desktop CLI 绝对路径；新 PowerShell 会话中须重新赋值。

若 Windows 安装时配置过固定 CDP 入口，先读取 `%LOCALAPPDATA%\Codex Token Sidebar\CDP\setup-state.json`，只清理路径和 SHA-256 与记录一致的快捷方式、`codex-cdp.cmd` 和 `codex_cdp.py`；终端路线还需从当前用户的 Path 中移除 `CDP` 目录。最后删除状态文件，不清理同目录中的其他文件。
