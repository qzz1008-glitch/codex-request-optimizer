## 简体中文

适用于 Windows x64（64 位）。

本次更新重点改善升级过程中的连接连续性、启动恢复、图片配置兼容性和共享管理体验。

- **升级与连接保留**：完善本地中转、借入额度及网络共享在升级切换和回滚时的交接，保留有效凭据与进行中的连接；修复 Windows 升级结果文件被占用等问题。
- **旧版迁移提示**：不支持无中断切换的旧服务会被保留，升级器不会强行停止它。请按提示主动退出旧版，或停止旧版共享借入／借出后重新运行安装包。
- **启动与配置恢复**：减少冷启动等待，新请求重新读取已变化的代理配置；完善待生效配置提示、服务重启确认和启动诊断。
- **图片与请求兼容性**：依据已安装 Codex 的实际能力迁移原生图片开关，保留其他配置；改善共享额度转发中的原生协议、增量响应事件及用量识别。
- **共享管理**：改进默认凭据命名、最近连接时间、有效期与每周刷新布局；保留共享页签选择，统一表格列宽并减少无用计数。
- **分析界面与安装窗口**：优化会话归属、标题请求分组和三栏布局；趋势默认显示 1 小时，安装升级窗口使用更紧凑的步骤与进度展示。
- **文档与验证**：补充中英文界面示意，完整单测分组并行并与 EXE 验证同步执行，保留全部发布检查。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；正常升级时，新请求交给新版服务，处理中请求由原服务继续完成。配置变更后按界面提示重启 ChatGPT App；验证 WSL 接入时，请先启动相应发行版。

## English

For Windows x64 (64-bit).

This release improves connection continuity during upgrades, startup recovery, image configuration compatibility, and sharing management.

- **Upgrade continuity**: improve handover and rollback for the local relay, borrowed quota, and shared network access, preserving valid credentials and active connections. Fix Windows sharing violations affecting upgrade result files.
- **Legacy migration**: preserve running services that cannot switch without interruption instead of forcibly stopping them. Follow the prompt to exit the older version, or stop its borrowed or lent sharing services, before running the installer again.
- **Startup and configuration recovery**: reduce cold-start waits and refresh changed proxy settings for new requests. Improve pending-configuration notices, service restart confirmation, and startup diagnostics.
- **Image and request compatibility**: migrate native image switches according to the capabilities of the installed Codex runtime while preserving other settings. Improve native protocol forwarding, incremental response events, and usage recognition through quota sharing.
- **Sharing management**: improve default credential names, recent connection times, expiry controls, and weekly reset layouts. Preserve the selected sharing tab, align table columns, and remove unnecessary counts.
- **Analysis and installation interface**: refine session relationships, title-request grouping, and the three-pane layout. Default trends to one hour and use a more compact installation and upgrade progress display.
- **Documentation and validation**: add bilingual interface illustrations. Run the complete test suite in parallel shards alongside EXE verification while retaining every release check.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. During a normal upgrade, the new service handles new requests while the previous service finishes requests already in progress. Restart ChatGPT App when prompted after configuration changes. Start the relevant WSL distribution before validating WSL connection support.
