## 简体中文

适用于 Windows x64（64 位）。

0.9.3 改进首次接入、跨设备解图、请求大小保护和趋势浏览。

- **首次安装**默认开启大小保护、容量重试和异常重试。独立图片工具配置成功后开启解图优化，首次打开时可确认是否接入 Codex。
- **独立解图**现可用于 WSL 和已接入的 SSH 服务器。解析优先使用发起轮次的模型，待分析的图片会保留给独立解图会话。
- **自动压缩**在快速压缩后的请求仍超过上限时尝试更高等级的无损压缩，采用较小的请求包执行大小保护。
- **连接管理**修复 Windows 代理选择和 Codex 回环连接问题。普通 Linux SSH 重连时会提示确认，重启对应后台可能中断该连接上正在运行的任务。
- **趋势浏览**可在选中会话时将两张图一起移到对应时段。密集图表在界面切换后绘制，让切换更流畅。
- **设置与共享**统一输入框、数字和单位的布局。共享工作区改为按需开启。

运行正式版 **CodexPacketInspector.exe** 安装或升级，也可在设置中检查更新。升级保留已有记录、设置和开发或正式模式。

## English

For Windows x64 (64-bit).

0.9.3 improves first-run setup, image analysis across devices, request size protection, and trend browsing.

- **First-time installations** enable size protection, capacity retries, and request error retries by default. Image analysis optimization turns on when the separate image tool is configured successfully. On first launch you can confirm whether to connect Codex.
- **Separate-session image analysis** now supports WSL and connected SSH servers. It prefers the model from the calling turn and keeps the images available to the analysis session.
- **Automatic compression** tries a higher lossless level if fast compression leaves a request over the limit. Size protection uses the smaller of the two results.
- **Connection management** fixes Windows proxy selection and Codex loopback connections. Standard Linux SSH reconnections ask for confirmation, since restarting the corresponding backend may interrupt tasks still running on that connection.
- **Trend browsing** moves both charts to the selected session's time range. Dense charts render after the view transition for smoother switching.
- **Settings and sharing** align input controls, numbers, and units. The sharing workspace can now be enabled when needed.

Run the release **CodexPacketInspector.exe** to install or upgrade, or check for updates in Settings. Upgrading preserves records, settings, and the installed development or release mode.
