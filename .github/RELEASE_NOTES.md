## 简体中文

适用于 Windows x64（64 位）。

0.9.1 改进请求正文阅读、会话识别和趋势浏览，并重新整理连接与设置界面。

- **请求正文**：默认以对话气泡显示已采集的文字，可切换到脱敏 JSON。支持在原位置搜索，并复制或导出当前视图。请求概况、大小构成和变化明细也重新分组。
- **会话识别**：优先显示 Codex 会话名称，缺少原生名称时使用已采集的用户文字作为标题。根据请求实际经过的连接识别本机、WSL 和 SSH 来源，改进独立对话与辅助请求的分组。
- **记录与趋势**：两种视图共用会话列表和筛选，切换时保留当前会话。趋势图收起较长的空档，记录时间范围与会话数移到主界面底部。
- **连接与设置**：顶栏统一管理本机和 SSH 服务器连接。设置按用途分组，可在上限旁查看已保存的记录数和已用空间，并单独检查更新或安装新版。
- **解图优化**：改为一个开关。开启时使用独立会话解析图片，关闭时使用 Codex 原生图片解析。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求。如需暂停旧版服务，升级器会先请求确认。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

0.9.1 improves request body reading, session identification, and trend browsing, and reorganizes connections and Settings.

- **Request bodies**: read captured text in a conversation view by default, or switch to redacted JSON. Search in place and copy or export the current view. The overview, size breakdown, and changes are also regrouped.
- **Session identification**: use the Codex session name when available and captured user text as a fallback. Identify local, WSL, and SSH origins from the connection used by each request, with improved grouping of standalone conversations and helper requests.
- **Records and trends**: both views share the session list and filters, preserving the selected session when switching. The charts fold longer idle periods. The record date range and session count appear at the bottom of the main window.
- **Connections and Settings**: manage local and SSH server connections from one toolbar control. Related settings are grouped together, with record counts and used space next to their limits and separate actions for checking and installing updates.
- **Image analysis optimization**: a single switch selects separate-session image analysis when on and native Codex image analysis when off.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If a legacy service needs to pause, the installer asks for confirmation first. Reload or restart the Codex client when prompted after configuration changes.
