## 简体中文

适用于 Windows x64（64 位）。

本次更新新增局域网额度与网络共享，并完善图片请求优化、压缩策略、记录浏览和网络错误诊断。

- **额度与网络独立共享**：可分别借出或借入 Codex 额度、网络出口，自由组合自己的额度与同事的网络，或同事的额度与自己的网络。支持多张具名凭据、历史列表和拖动排序；未启用借入出口时默认使用本机网络。
- **共享凭据与额度管理**：按凭据设置有效期、美元参考费用上限和每周刷新日，查看累计用量，并可调整本期已用额度。额度凭据绑定单个客户端，支持停用与删除；连接校验证书指纹，本机敏感配置使用 Windows 用户加密保护。参考费用不代表 ChatGPT 订阅的实际扣费或余额。
- **图片请求传输与大小保护**：识别图片生成、图片编辑请求，对其 JSON 进行 zstd 无损传输压缩；图片压缩等级独立设置，默认 6 级。开启请求大小保护后，仍超限的输入图片可按比例缩小，并显示处理前后分辨率；缩图可能损失细节，原文件保留。
- **压缩策略与独立图片解析**：优化对话请求的自动压缩档位及等级选择提示，完善工具结果中内嵌图片的隔离处理，修复 Windows 下独立解析的命令传参与工具配置兼容性。
- **记录与趋势**：可配置的请求记录上限提高到 100,000 条；趋势图聚焦对话与原生上下文压缩请求，避免图片生成、编辑流量混入对话趋势。
- **桌面界面**：改进共享凭据表格、行内编辑、日期选择和操作反馈，更新 Logo，并修复托盘图标在不同 DPI 下的显示；双击托盘图标打开主窗口。
- **错误诊断**：请求详情显示更具体的网络失败原因，区分代理 CONNECT、TLS、上传和等待响应阶段，保留网关状态及关联诊断信息；诊断记录自动按时间、条数和大小回收。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；正常升级时，新请求交给新版服务，处理中请求由原服务继续完成。应用重启后，共享需手动重新开启，未失效的原有凭据可继续使用。

## English

For Windows x64 (64-bit).

This release adds LAN quota and network sharing and improves image request optimization, compression policies, record browsing, and network error diagnostics.

- **Independent quota and network sharing**: lend or borrow Codex quota and network exits separately. Combine your own quota with a colleague's network, or a colleague's quota with your own network. Manage multiple named credentials, saved history, and drag-and-drop ordering. Your local network is used when no borrowed exit is enabled.
- **Sharing credentials and quota controls**: set expiry dates, reference spending limits in USD, and weekly reset days per credential. View cumulative usage and adjust usage for the current period. Quota credentials bind to a single client and can be disabled or deleted. Connections verify certificate fingerprints, and sensitive local settings use Windows user encryption. Reference costs do not represent actual ChatGPT subscription charges or balances.
- **Image request transport and size protection**: recognize image generation and editing requests and apply lossless zstd compression to their JSON. Image compression has its own level setting, defaulting to level 6. With request size protection enabled, oversized input images can be resized proportionally, with before-and-after resolutions shown. Resizing may lose detail; original files are retained.
- **Compression policies and isolated image analysis**: refine automatic compression phases and level-selection guidance for conversation requests. Improve isolation of inline images in tool results and fix command argument handling and tool configuration compatibility for isolated analysis on Windows.
- **Records and trends**: raise the configurable request record limit to 100,000. Trend charts focus on conversation and native context-compaction requests, keeping image generation and editing traffic out of conversation trends.
- **Desktop interface**: improve sharing credential tables, inline editing, date selection, and action feedback. Update the logo and fix tray icon rendering at different DPI settings. Double-click the tray icon to open the main window.
- **Error diagnostics**: show more specific network failure causes in request details, distinguishing proxy CONNECT, TLS, upload, and response-wait stages. Preserve gateway status and related diagnostic information. Diagnostic records are automatically pruned by age, count, and size.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. During a normal upgrade, the new service handles new requests while the previous service finishes requests already in progress. Sharing must be enabled manually after an application restart; existing credentials that have not expired or been revoked can still be used.
