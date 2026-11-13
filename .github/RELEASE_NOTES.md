## 简体中文

适用于 Windows x64（64 位）。

本次更新新增可选的请求异常重试，并改善上下文处理、共享凭据状态和界面反馈。

- **请求异常重试**：可拦截仅含 `Bad Request` 的通用 400，并额外重试 1～9 次。默认关闭，启用后默认重试 3 次；明确的字段或参数错误仍原样返回。
- **上下文处理**：改进原生 agent 上下文的识别，在超限保护中保留这类上下文，避免误当作普通工具输出。
- **共享凭据**：用独立的黄色状态区分已过期凭据，启用开关与有效期分别管理；过期不再隐式改变启用状态或关闭仍启用的共享通道，并统一操作按钮和状态布局。
- **界面反馈**：精简设置保存反馈，完成的步骤及时移除，减少重复状态；调整窗口控制按钮的位置与标题栏间距。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。运行 EXE 安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发／正式模式。支持无缝交接的版本会保留正在处理的请求；若旧版服务需要暂停，升级前会明确请求确认，暂停期间的请求可能失败。配置变更后按提示重新加载或重启 Codex 客户端。

## English

For Windows x64 (64-bit).

This release adds optional request error retries and improves context handling, sharing credential states, and interface feedback.

- **Request error retries**: optionally intercept a generic 400 containing only `Bad Request` and make 1–9 additional attempts. Disabled by default, with three retries when enabled; explicit field or parameter errors pass through unchanged.
- **Context handling**: improve recognition of native agent context, preserve it during overflow protection, and avoid treating it as ordinary tool output.
- **Sharing credentials**: show expired credentials with a distinct yellow status and manage enablement separately from expiry. Expiration no longer implicitly changes enablement or closes sharing channels that remain enabled. Align action buttons and status layouts.
- **Interface feedback**: simplify settings-save feedback, remove completed steps promptly, and reduce redundant status messages. Adjust window control placement and title-bar spacing.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Versions supporting seamless handover preserve active requests. If legacy services need to pause, the installer asks for confirmation first; requests during the pause may fail. Reload or restart the Codex client when prompted after configuration changes.
