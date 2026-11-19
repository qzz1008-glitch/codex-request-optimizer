## 简体中文

适用于 Windows x64（64 位）。

0.9.0 新增 SSH 远程设备接入与出口代理设置，并改进请求分析、会话识别和数据存储体验。

- **SSH 远程接入**：管理本机（含已检测到的 WSL 配置）与已保存的 SSH 服务器接入。远端 Codex 请求可经 SSH 交给 Windows 优化器处理，服务器无需安装优化器；各 SSH 服务器可分别接入、断开与重连，并验证配置是否生效。
- **出口代理**：支持自动检测或手动配置 HTTP/HTTPS 代理，可在保存前测试连接，设置对新请求生效。
- **请求与会话分析**：整合请求信息与处理详情，展示实际压缩方式、等级和图片改写前后的大小；改进真实 Token 用量保留、远程会话名称与归属识别，支持按远端 IP 搜索。
- **数据存储**：统一数据目录与图片缓存管理，改善历史图片路径保留和过期缓存清理，支持整数 MB 的记录存储上限设置。
- **界面与稳定性**：调整设备连接、侧栏和端口设置，改善托盘恢复、趋势曲线和错误状态更新。
- **Linux 验证版**：源码新增无界面服务器构建与运行方式，目前面向 EulerOS 3.0 x86_64 验证。本次公开下载仍提供 Windows EXE，Linux 构建与使用步骤见使用指南。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求；若旧版服务需要暂停，升级前会明确请求确认，暂停期间的请求可能失败。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

0.9.0 adds SSH remote device connections and outbound proxy settings, and improves request analysis, session identification, and data storage.

- **SSH remote connections**: manage this computer, including detected WSL configurations, and saved SSH servers. Route remote Codex requests through the Windows optimizer over SSH without installing the optimizer on the server. Connect, disconnect, or reconnect individual SSH servers and verify that configuration changes have taken effect.
- **Outbound proxies**: automatically detect a proxy or configure an HTTP/HTTPS address manually. Test connectivity before saving; changes apply to new requests.
- **Request and session analysis**: combine request information and processing details, show the actual compression method and level, and distinguish image sizes before and after rewriting. Improve retention of real Token counts, remote session names and ownership, and search by remote IP address.
- **Data storage**: unify the data directory and image cache management, improve historical image path retention and expired cache cleanup, and accept whole-MB record storage limits.
- **Interface and reliability**: refine device connections, the sidebar, and port settings. Improve tray activation, trend lines, and error status updates.
- **Linux validation build**: source support now includes headless server builds and operation, currently under validation on EulerOS 3.0 x86_64. Public downloads in this release remain Windows EXEs; see the usage guide for Linux build and usage steps.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If legacy services need to pause, the installer asks for confirmation first; requests during the pause may fail. Reload or restart the Codex client when prompted after configuration changes.
