## 简体中文

**平台：Windows x64（64 位）。**

新增会话 Token 趋势视图，优化桌面顶栏与设置布局，并修复超限压缩对多次工具结果的处理。

- **Token 趋势**：在同一时间轴查看会话 Token 变化，随新请求更新，支持历史数据分页与按需补齐。
- **桌面布局**：视图切换居中，重新安排顶栏控件并降低传输模式按钮高度；设置分区增加层次，加载图标与标签一起居中。
- **设置修复**：修复移除预览功能后语言设置无法保存的问题，让更新按钮和状态始终处于稳定的一行。
- **请求详情**：移除图片路径旁的复制按钮，简化图片信息展示。
- **超限保护**：同一次工具调用返回多条结果或延迟通知时，始终将整组内容一起处理；复用压缩结果前重新检查分组边界，避免拆开工具调用与后续结果。
- **构建完整性**：将 CommonJS 前端测试纳入源码快照，修复完整构建被新增测试文件阻止的问题。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**，不提供单独升级包或旧文件名别名。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式。

## English

**Platform: Windows x64 (64-bit).**

This release adds session token trends, refines the desktop header and settings layout, and fixes over-limit compaction of tool calls with multiple results.

- **Token trends**: view session token changes on a shared timeline, updated as requests arrive, with paged history and on-demand backfill.
- **Desktop layout**: center view switching, rearrange header controls, and reduce the height of transmission mode buttons. Make settings sections clearer and center loading icons together with their labels.
- **Settings fixes**: fix language settings failing to save after preview removal, and keep update controls and status on one stable row.
- **Request details**: remove copy buttons beside image paths to simplify image information.
- **Over-limit protection**: keep all results and delayed notifications from the same tool call together. Recheck group boundaries before reusing a compacted result so calls and later outputs remain paired.
- **Build completeness**: include CommonJS frontend tests in source snapshots, fixing full builds blocked by the new test file.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Separate upgrade packages and legacy filename aliases are not provided. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode.
