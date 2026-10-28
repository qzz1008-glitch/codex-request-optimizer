## 简体中文

适用于 Windows x64（64 位）。

本次更新托盘面板、趋势时间轴、连接诊断、存储设置和请求大小保护。

- **托盘面板**：新增紧凑面板，查看会话、请求、上下文及压缩统计。改善首次打开速度、菜单定位和显示层级，统一字体与间距，并兼容已有的后台入口。
- **连续趋势时间轴**：省去无请求长空档中的大部分时间，在两张图中同步标记跳过的时段；节点保留实际时间，支持平移、缩放和悬停查看，机制标记沿用对应会话的颜色。
- **接入与错误诊断**：点击“接入”时检查配置、环境及网络地址；请求失败时记录连接、代理、TLS、上传及响应等待等阶段信息，便于定位问题。诊断日志不保存正常对话输出或请求正文。
- **存储位置设置**：集中配置日志、缓存和图片目录，支持打开目录及恢复默认路径。日志、缓存目录修改在下次启动本软件时生效，原目录数据保留。
- **图片会话归组**：独立图片解析请求关联已知主会话，即使主会话名称尚未加载，也能保持正确归组。
- **原生压缩请求保护**：根据实际上传大小为前置处理预留空间，精简前置摘要，改善接近网关上限时的压缩请求转发。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；正常升级时，新请求交给新版服务，处理中请求由原服务继续完成。

## English

For Windows x64 (64-bit).

This release updates the tray panel, trend timeline, connection diagnostics, storage settings, and request-size protection.

- **Tray panel**: add a compact panel showing session, request, context, and compaction statistics. Improve first-open speed, menu positioning, and stacking order, standardize typography and spacing, and support existing background entry points.
- **Continuous trend timeline**: omit most of long idle periods and mark the skipped intervals consistently in both charts. Keep actual request timestamps, support panning, zooming, and hover details, and use the corresponding session color for mechanism markers.
- **Connection and error diagnostics**: check configuration, environment, and network addresses when Connect is clicked. Record connection, proxy, TLS, upload, and response-wait stages when requests fail to help locate problems. Diagnostic logs do not store normal conversation output or request bodies.
- **Storage location settings**: configure log, cache, and image directories together, with actions to open directories or restore default paths. Changes to log and cache directories take effect on the next application start; data in previous directories is retained.
- **Image-session grouping**: associate independent image-analysis requests with their known parent session even before the parent's title has loaded.
- **Native compaction request protection**: reserve headroom for preprocessing based on actual upload size and shorten preliminary summaries, improving forwarding near the gateway limit.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. During a normal upgrade, the new service handles new requests while the previous service finishes requests already in progress.
