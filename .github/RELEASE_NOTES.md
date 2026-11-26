## 简体中文

适用于 Windows x64（64 位）。

0.9.5 带来独立的对话与图片压缩设置、更快的桌面启动，以及新的版本和主题设置。

- **传输压缩** 可分别为对话和图片选择手动或自动 zstd 压缩。对话请求接近预测保护阈值时，自动尝试更高等级的无损压缩，并用更小的结果判断是否触发大小保护。
- **连接与重启提示** Windows、WSL 和 SSH 使用一致的 Codex 接入规则。设置页和连接菜单提示哪些改动需要重新加载，恢复设置后会清除相应提示。
- **桌面与主题** 主窗口可在中转就绪前打开，缩短启动等待。浅色和深色主题可即时预览并保存。设置中可以查看对应模型的默认上下文窗口和压缩阈值。
- **检查更新** 定期自动检查新版本，可在设置中阅读中英文版本记录。打开较旧的安装文件时会保留较新的本机版本。
- **请求分析** 大小构成直接显示变化量，推理条目可分别展开密文与其他内容的变化。点击趋势图可定位到对应会话。

运行正式版 **CodexPacketInspector.exe** 安装或升级，也可在设置中检查更新。升级保留已有记录、设置和安装模式。支持无缝交接的版本可在升级期间继续处理在途请求。

## English

For Windows x64 (64-bit).

0.9.5 adds independent conversation and image compression controls, faster desktop startup, and new version and theme settings.

- **Transfer compression** Choose manual or automatic zstd separately for conversations and images. Near the predictive protection threshold, automatic conversation compression tries a higher lossless level and uses the smaller result to decide whether to trigger size protection.
- **Connections and restart prompts** Windows, WSL, and SSH follow the same Codex connection rules. Settings and the connection menu show which changes require a reload and clear the corresponding prompts when changes are reverted.
- **Desktop and themes** The main window can open while the relay starts. Preview and save either light or dark mode, and see the default context window and compaction threshold for each model in Settings.
- **Updates** The app checks for new versions periodically and shows release notes in Chinese and English in Settings. Opening an older installer preserves the newer installed version.
- **Request analysis** View changes directly in the size breakdown and expand reasoning entries to see separate changes for encrypted and other content. Click a point in a trend chart to jump to the corresponding session.

Run the release **CodexPacketInspector.exe** to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed mode. Versions that support seamless handover can continue processing in-flight requests during the upgrade.
