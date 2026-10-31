## 简体中文

适用于 Windows x64（64 位）。

本次更新增加 WSL 接入验证支持，并改进连接恢复、安装升级、响应兼容性和操作反馈。

- **WSL 接入（验证版）**：检测正在运行的本机 WSL 发行版，验证连通后接入同一个 Windows 中转；断开时恢复相关配置。支持读取 WSL 会话名称和父子关系，修复探测及历史记录发现问题。
- **连接检查与恢复**：已接入的软件在启动和升级后自动检查连接、补齐必要设置，统一 Windows 与 WSL 配置变更后的重启提示。
- **Codex 与响应兼容性**：改进已安装 Codex CLI 的自动发现，兼容更多 Responses 事件与用量字段，修复部分请求完成状态、Token 统计和容量重试诊断的识别问题。
- **托盘与安装升级**：托盘菜单新增“重启服务”；安装与升级窗口采用居中的步骤进度界面，显示实际执行状态。
- **操作反馈与共享界面**：操作状态显示在对应控件附近；优化共享凭据启停位置、按钮布局和每周刷新列显示，并在更新设置中加入仓库链接。
- **文档整理**：精简中英文 README，保留核心功能、简短使用步骤和实现概览，详细说明归入使用指南。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；正常升级时，新请求交给新版服务，处理中请求由原服务继续完成。配置变更后按界面提示重启 ChatGPT App；首次验证 WSL 接入时，请先启动相应发行版。

## English

For Windows x64 (64-bit).

This release adds experimental WSL connection support and improves connection recovery, installation and upgrades, response compatibility, and action feedback.

- **WSL connection (experimental)**: detect running local WSL distributions and connect them to the same Windows relay after a connectivity check. Restore the relevant configuration on disconnect. Read WSL session names and parent-child relationships, with fixes for probing and history discovery.
- **Connection checks and recovery**: automatically check existing connections and restore required settings after startup or upgrades. Unify restart prompts after Windows and WSL configuration changes.
- **Codex and response compatibility**: improve discovery of installed Codex CLI runtimes and support more Responses events and usage fields. Fix recognition issues affecting completion status, token statistics, and capacity-retry diagnostics.
- **Tray and installation**: add “Restart service” to the tray menu. Use a centered installation and upgrade window with step-by-step progress reflecting actual execution status.
- **Action feedback and sharing interface**: show action status beside the relevant controls. Improve sharing credential activation controls, button layouts, and weekly refresh columns, and add a repository link to update settings.
- **Documentation**: simplify both READMEs to focus on core features, brief usage steps, and an implementation overview. Keep detailed instructions in the usage guide.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. During a normal upgrade, the new service handles new requests while the previous service finishes requests already in progress. Restart ChatGPT App when prompted after configuration changes. Start the relevant WSL distribution before trying WSL connection support for the first time.
