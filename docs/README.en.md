# ⚡ Codex Request Optimizer

[简体中文](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/README.md) · **English**

A local request analysis and optimization tool for Codex on Windows x64 (64-bit). Fit more conversation context within limits such as **1 MB per request** through compression and size protection.

**[⬇️ Download](https://github.com/qzz1008-glitch/codex-request-optimizer/releases)** · **[📖 Usage guide](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md)**

## ✨ Features

- **🗜️ Lossless compression**: reduce upload sizes for conversations, image generation, and image editing while retaining their content. Choose automatic compression or a manual level.
- **🛡️ Size protection**: predictive protection triggers context compaction early; overflow protection condenses history before forwarding oversized requests, avoiding gateway 403 errors caused by request size.
- **🧠 Context settings**: adjust the context window and automatic compaction threshold, or restore model defaults.
- **🖼️ Image optimization**: choose Native, Disable, or Optimize mode. Optimization uses a separate analysis session to reduce image content in the main context. Size protection can also downscale oversized image requests, which may lose detail.
- **🔁 Capacity retries**: let Codex wait and retry automatically when a model is at capacity. Set the retry count and delay.
- **🔎 Analysis and trends**: inspect request sizes, token usage, errors, and redacted JSON by session. View trends, request changes, and protection events.
- **🤝 LAN sharing**: lend or borrow Codex quota and network exits independently. Manage credentials, expiry, reference spending limits, and weekly resets.

Capacity retries handle: `Selected model is at capacity. Please try a different model.`

## 🚀 Usage

1. Download `CodexPacketInspector.exe` from [Releases](https://github.com/qzz1008-glitch/codex-request-optimizer/releases) and run it to install. No Python setup is needed.
2. Click Connect and adjust compression, protection, and image options in Settings.
3. Restart your Codex client when prompted. Later, check for updates in Settings or run a newer EXE to upgrade.

Includes Chinese/English language switching, tray operation, and start on login. Upgrades preserve records and settings.

Existing Codex configurations in WSL can also be connected as a validation feature. See the [usage guide](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md) for sharing, WSL, and other detailed instructions.

## ⚙️ Implementation overview

A local Python relay connects through Codex's standard provider configuration, using zstd for lossless transport compression, SQLite for records, and WebView2 for the desktop interface. Codex manages the separate image-analysis tool.

Request records stay on the local machine. Captured records omit authentication values; JSON views and downloads use redacted copies.

## 📄 License

This is an independent third-party tool released under the **[MIT License](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/LICENSE)**. Third-party component licenses are included in `THIRD_PARTY_NOTICES.txt` with the release.
