# Codex Token Sidebar：Agent 安装入口

先判断 **Codex Desktop 所在电脑的操作系统**。仓库应已克隆，当前目录为包含 `.agents/plugins/marketplace.json` 的仓库根目录。

Windows Desktop 若把 Agent 配置为 WSL，先在 Desktop 设置中将 **Agent environment** 切换到 **Windows native** 并重启，再进入 Windows 路线。以 Desktop 宿主平台判定路线；WSL 内的 `platform.system()` 返回 Linux 不能代表宿主平台。

| Desktop 宿主平台 | 进入的安装路线 |
| --- | --- |
| macOS（Darwin） | [macOS 安装路线](install/macos.md) |
| Windows | [Windows 原生安装路线](install/windows.md) |

只读取并执行选中的路线，以该路线末尾的验收条件结束。其他宿主平台报告不支持。
