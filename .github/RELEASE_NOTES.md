## 简体中文

适用于 Windows x64（64 位）。

本次更新集中改善三栏界面、请求机制展示、会话列表和接入恢复体验。

- **界面布局**：统一三栏标题与内容边界，优化底部留白、JSON 浏览区和会话卡片间距。记录占用与请求数移至底部右侧，调整品牌文字与底栏配色，最小窗口为 1280 × 720。
- **请求详情**：以“请求概况”和“请求处理”组织信息，集中展示大小预测、超限压缩、容量重试、解图优化四种机制，用符号与配色标记状态，并突出已触发的具体字段。图片缩放信息移至大小构成详情。
- **会话列表**：名称暂时无法读取时保留已加载的名称；缺名子任务按明确的父会话关系归组。调整关联线、搜索框和卡片对齐。
- **接入恢复**：暂时同步失败时自动尝试恢复，减少正常处理过程和等待 WSL 启动时的提示。持续失败时在现有提示中说明错误和日志位置，并支持导出脱敏诊断。
- **兼容修复**：访问已停用的旧图片 HTTP 入口时稳定返回停用提示，避免 Windows 因连接提前关闭而报错。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求；若旧版服务需要暂停，升级前会明确请求确认，暂停期间的请求可能失败。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

This release improves the three-pane interface, request-mechanism details, session lists, and connection recovery.

- **Interface layout**: align headings and content across all three panes, refine bottom spacing, the JSON viewer, and session cards. Move storage usage and request counts to the bottom-right footer, refresh the wordmark and footer colors, and use a minimum window size of 1280 × 720.
- **Request details**: organize information into Request overview and Request processing. Show size prediction, over-limit compaction, capacity retries, and image-analysis optimization with symbols and colors, highlighting fields that triggered a mechanism. Move image-downscaling information into size details.
- **Session lists**: preserve loaded names when native metadata is temporarily unavailable, and group unnamed subtasks using explicit parent relationships. Refine connection lines, search alignment, and card spacing.
- **Connection recovery**: automatically retry temporary synchronization failures while keeping routine recovery and waits for WSL startup quiet. Persistent failures use the existing feedback area with error and log locations, plus a redacted diagnostics export.
- **Compatibility fix**: reliably return the retirement message for the former image HTTP endpoint, avoiding Windows connection-aborted errors caused by closing too early.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If legacy services need to pause, the installer asks for confirmation first; requests during the pause may fail. Reload or restart the Codex client when prompted after configuration changes.
