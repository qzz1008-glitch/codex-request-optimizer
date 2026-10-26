## 简体中文

适用于 Windows x64（64 位）。

本次改进趋势分析、容量重试诊断、接入切换与在线升级体验。

- **双趋势图**：输入 Token 与请求包大小共用时间轴，悬停时同步查看时间和两项数值，并定位对应会话。会话曲线保持连续，保护、重试及原生压缩标记使用会话颜色，重叠标记与实时刷新时的悬停显示更清晰。
- **重试诊断**：记录容量错误识别、重试安排、实际尝试和最终结果；请求详情说明是否触发重试、为何未重试，以及取消或异常等终止原因。
- **接入与断开**：统一顶栏连接状态与确认提示。接入时由本软件管理请求压缩；断开时恢复原上游和 Codex 原生请求压缩。配置变化后提示重新加载客户端，失败时回滚本次配置修改。
- **升级可靠性**：升级时刷新桌面入口、托盘和系统安装信息，保留已有数据、设置及运行模式；安全清理已停用的旧版本，仍在运行或处理请求的版本继续保留。
- **会话与界面**：侧栏关联线仅显示已确认的子任务关系；优化趋势页会话搜索、标记大小、悬停提示和控件布局，减少重复说明。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式。

## English

For Windows x64 (64-bit).

This release improves trend analysis, capacity retry diagnostics, connection switching, and online upgrades.

- **Dual trend charts**: input tokens and request sizes share a timeline. Hover to inspect the time and both values together and locate the corresponding session. Session curves stay continuous; protection, retry, and native compaction markers use session colors, with clearer overlapping markers and hover details during live updates.
- **Retry diagnostics**: record capacity error detection, scheduled retries, actual attempts, and final outcomes. Request details explain whether a retry occurred, why it was skipped, and whether cancellation or an exception ended processing.
- **Connect and disconnect**: unify header connection states and confirmation prompts. The optimizer manages request compression while connected; disconnecting restores the original upstream and native Codex request compression. Configuration changes prompt a client reload, and failed changes are rolled back.
- **Reliable upgrades**: refresh desktop entry points, the tray, and system installation information while preserving data, settings, and the installed mode. Safely clean up retired versions while retaining versions that are still running or handling requests.
- **Sessions and interface**: sidebar links show only confirmed subagent relationships. Refine trend session search, marker sizes, hover callouts, and controls while removing redundant explanatory text.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode.
