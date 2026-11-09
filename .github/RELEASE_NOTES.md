## 简体中文

适用于 Windows x64（64 位）。

本次更新集中改善接入兼容性、旧会话延续使用和客户端状态提示。

- **接入兼容**：统一接入与断开的配置检查，兼容隐式 Codex 账户、旧式登录文件及 WSL 配置；保留自定义上游和已有认证设置。
- **旧会话兼容**：接入及升级时检查兼容的旧会话 provider 配置，减少旧会话因配置变化而无法继续使用的问题，保留会话内容。
- **重启提醒**：检测 ChatGPT App 完整退出并重新启动后，自动清除对应提醒；尚未生效的其他配置继续保留提示。
- **接入异常**：在顶栏明确显示异常，悬停查看原因，点击打开日志目录，方便定位问题。
- **托盘退出**：确认退出并成功停止中转后，同时关闭桌面窗口，避免留下旧窗口。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求；若旧版服务需要暂停，升级前会明确请求确认，暂停期间的请求可能失败。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

This release improves connection compatibility, continued use of existing chats, and client status messages.

- **Connection compatibility**: unify configuration checks for connecting and disconnecting, support implicit Codex account providers, legacy login files, and WSL configurations, and preserve custom upstreams and existing authentication settings.
- **Existing chats**: check compatible legacy chat provider settings when connecting or upgrading, reducing failures caused by configuration changes while preserving conversation content.
- **Restart reminders**: automatically clear the corresponding reminder after detecting that ChatGPT App has fully exited and restarted; retain reminders for other pending changes.
- **Connection errors**: show a clear error in the header. Hover for the reason and click to open the log folder for troubleshooting.
- **Tray exit**: after a confirmed exit successfully stops the relay, close the desktop window as well to avoid leaving an old window open.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If legacy services need to pause, the installer asks for confirmation first; requests during the pause may fail. Reload or restart the Codex client when prompted after configuration changes.
