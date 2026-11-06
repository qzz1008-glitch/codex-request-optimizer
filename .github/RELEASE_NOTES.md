## 简体中文

适用于 Windows x64（64 位）。

本次更新改善会话列表加载、重启提示和图片文件操作，并整理请求详情中的压缩信息。

- **会话列表**：优化大量记录下的会话信息读取，减少加载超时；加载指示居中显示。
- **重启提示**：明确需要重启或重新连接的客户端，悬停可查看待生效的配置及原因。
- **图片文件**：新生成的附件和剪贴板副本采用更短的文件名，界面显示实际名称；支持打开所在文件夹并选中对应图片。
- **请求详情**：仅在有相应数据时显示文本与图片压缩明细；长错误消息旁的字段名称保持横向排列。
- **界面示意**：更新中英文设置图，展示默认上下文、默认压缩阈值和已开启的图片优化。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求；若旧版服务需要暂停，升级前会明确请求确认，暂停期间的请求可能失败。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

This release improves session loading, restart reminders, and image-file actions, and refines compression information in request details.

- **Session lists**: improve session-metadata queries when many records are stored, reducing loading timeouts; center the loading indicator.
- **Restart reminders**: identify the client that needs restarting or reconnecting. Hover to see pending configuration changes and their reasons.
- **Image files**: use shorter filenames for newly generated attachments and clipboard copies, and show their actual names. Open the containing folder with the corresponding image selected.
- **Request details**: show text and image compression details only when the corresponding data exists; keep field labels horizontal beside long error messages.
- **Interface previews**: update the Chinese and English settings images to show default context settings, the default compaction threshold, and image optimization enabled.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If legacy services need to pause, the installer asks for confirmation first; requests during the pause may fail. Reload or restart the Codex client when prompted after configuration changes.
