# ⚡ Codex 请求优化器

**简体中文** · [English](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/README.en.md)

Codex 的本地请求分析与优化工具，运行于 Windows x64（64 位）。通过压缩和大小保护，在 **1 MB 请求上限**下容纳更多上下文。

**[⬇️ 下载](https://github.com/qzz1008-glitch/codex-request-optimizer/releases)** · **[📖 使用指南](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md)**

## ✨ 主要功能

- **🗜️ 无损压缩**：减小对话、图片生成和编辑请求的上传体积，保留原有内容；支持自动选档或手动压缩等级。
- **🛡️ 大小保护**：预测保护提前触发上下文压缩，超限保护精简历史后再转发，避免请求过大导致网关 403。
- **🧠 上下文配置**：调整上下文窗口和自动压缩阈值，也可恢复模型默认值。
- **🖼️ 图片优化**：支持原生、禁用、优化三种模式。优化模式用独立会话解析图片，减少主会话占用；大小保护还可对超限图片请求缩图处理（可能损失细节）。
- **🔁 容量重试**：遇到模型容量不足时，让 Codex 自动等待重试，次数和间隔可设置。
- **🔎 分析与趋势**：按会话查看请求大小、Token 用量、错误和脱敏 JSON，观察趋势、对比变化及保护触发情况。
- **🤝 局域网共享**：额度与网络出口独立借入或借出，支持凭据管理、有效期、参考费用上限及每周刷新。

容量重试针对报错：`Selected model is at capacity. Please try a different model.`

## 🚀 使用

1. 从 [Releases](https://github.com/qzz1008-glitch/codex-request-optimizer/releases) 下载 `CodexPacketInspector.exe`，双击安装运行，无需 Python。
2. 点击“接入”，在设置中调整压缩、保护和图片选项。
3. 按提示重启 Codex 客户端；后续可在设置中检查更新，或运行新版 EXE 升级。

支持中英文切换、托盘驻留和登录后启动，升级保留已有记录与设置。

也可接入已有 WSL Codex 配置（验证功能）。共享、WSL 及其他详细操作见[使用指南](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md)。

## ⚙️ 实现概览

通过 Codex 标准 provider 配置接入本地 Python 中转，使用 zstd 无损传输压缩、SQLite 保存记录，WebView2 提供桌面界面。图片解析由 Codex 管理的独立工具完成。

请求记录保存在本机，认证值不写入采集记录，JSON 浏览与下载使用脱敏副本。

## 📄 许可证

本项目是独立第三方工具，采用 **[MIT License](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/LICENSE)**。第三方组件许可证见发布文件中的 `THIRD_PARTY_NOTICES.txt`。
