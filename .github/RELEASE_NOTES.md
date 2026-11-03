## 简体中文

适用于 Windows x64（64 位）。

本次更新改善独立图片解析、请求分类与详情、更新提醒，以及旧版共享服务的升级流程。

- **图片信息**：保留原图路径，避免识图结束、临时缩图被清理后无法打开；支持直接打开图片或所在目录。独立识图继续使用每次新建临时会话的方式。
- **请求分类与详情**：等待初始会话信息后再归组，修复图片解析与补全请求的筛选；精简子会话信息，并隐藏工具搜索请求中不适用的变化明细页签。
- **更新提醒**：重新打开设置时保留已检测到的可用更新，便于继续下载和安装。
- **旧版共享升级**：遇到不支持共享无缝交接的旧后台，可明确选择“暂停服务并升级”或“暂不升级”。确认后自动暂停、切换并恢复原共享端口、凭据与借入选择；取消则保留现有服务。升级失败时尝试恢复旧服务。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本正常完成正在处理的请求；确认暂停旧版服务时，正在进行及暂停期间发起的请求可能失败。更早、不支持固定入口的旧版仍需先主动退出再升级。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

This release improves independent image analysis, request classification and details, update notices, and upgrades from older sharing services.

- **Image details**: retain the original image path so images remain accessible after temporary resized uploads are cleaned up. Open images or their folders directly from the details panel. Independent analysis continues to use a fresh ephemeral conversation for each call.
- **Request classification and details**: wait for initial session metadata before grouping requests, fix image-analysis and complementary-request filters, simplify child-session information, and hide inapplicable change details for tool-search requests.
- **Update notices**: keep a detected available update visible when Settings is reopened, so downloading and installation can be continued.
- **Legacy sharing upgrades**: when an older worker cannot hand over sharing seamlessly, choose whether to pause services and upgrade or postpone. Confirmation automatically pauses, switches, and restores the existing sharing ports, credentials, and borrowed connections. Cancelling preserves the running services. Failed upgrades attempt to restore the previous services.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover let active requests finish normally; confirming a pause of legacy services may cause active requests and requests sent during the pause to fail. Much older versions without the fixed entry service still need to be exited before upgrading. Reload or restart the Codex client when prompted after configuration changes.
