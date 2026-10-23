# ⚡ Codex Request Optimizer

[简体中文](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/README.md) · **English**

A Windows tool for analyzing and optimizing Codex requests. Browse requests, token usage, and upload sizes by session, with lossless transport compression, request size protection, image handling, and automatic retries when a model is at capacity.

**[⬇️ Download the latest release](https://github.com/qzz1008-glitch/codex-request-optimizer/releases/latest)** · **[📖 User guide (Chinese)](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md)**

## ✨ Features

### 🔎 Session and request analysis

Browse request records by session to see what each request sent, how much space it used, and how it was handled.

- View session names, request times, models, response statuses, and elapsed times.
- Check context-window usage and input/output token counts; unavailable data is clearly marked.
- See how messages, tool outputs, reasoning content, and other parts contribute to request size.
- Explore structured request JSON, compare sizes before and after processing, and download a sanitized copy.

### 🗜️ Lossless zstd transport compression

Reduce upload size while preserving the original content and meaning.

Choose from **compression levels 1–17** and compare compressed sizes and estimated local processing times. Requests already using zstd can also be recompressed; the new body is used only when it is smaller.

### 🧠 Context settings

Adjust the context window and automatic compaction threshold used by the Codex client, or restore the model defaults.

Context settings and request size protection are configured separately, so you can adjust each to suit your needs.

### 🛡️ Request size protection

For request body limits such as **1M (1,000,000 bytes)**, set a custom size limit and use two protection mechanisms:

- **Predictive protection**: when a request approaches the configured threshold, prompt Codex to run native context compaction earlier, helping prevent later requests from exceeding the limit.
- **Over-limit protection**: when a request is already too large, turn completed history into a handover summary, retain necessary instructions and recent content, and check the resulting request size again. Later requests can reuse the corresponding result.

### 🔁 Automatic retries when a model is at capacity

When an explicit model capacity error occurs, retry automatically according to your settings, keeping the same model and request content.

### 🖼️ Image request optimization

Optionally save images from requests as local files and let the main session use their paths, reducing repeated transmission of image data in request history.

Image handling can be replaced with a tool that analyzes images in a separate session and returns the results to the main session, reducing the context occupied by images. These options can be enabled individually in Settings.

---

## 🚀 Usage

### Download and setup

1. Download `CodexPacketInspector.exe` from **[Releases](https://github.com/qzz1008-glitch/codex-request-optimizer/releases/latest)**.
2. Double-click the EXE and follow the prompts to install and connect it.
3. Adjust context settings and enable the protection, retry, and image options you need.
4. Restart your Codex client after connecting for the first time or changing the entry port.

The release EXE includes both the interface and the background service; no Python environment is required. Release assets also include the README, project license, and third-party licenses.

### Everyday use

Use the desktop window or open the interface in a browser. The default browser address is:

**[http://127.0.0.1:47844/](http://127.0.0.1:47844/)**

Closing the desktop window keeps the application in the system tray. Use the tray menu to exit the entire application. Language selection and launch at sign-in are available in Settings.

### Online updates

Check for updates and upgrade from Settings. The application verifies the download, updates the desktop window and background service, and preserves existing records and settings.

During a normal update, new requests go to the new service while the old service finishes requests already in progress.

### Local data

The default data directory is:

```text
%USERPROFILE%\.codex-packet-inspector
```

Request records, settings, configuration backups, and image attachments are stored locally. See the **[user guide (Chinese)](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/usage.md)** for detailed instructions.

---

## ⚙️ How it works

### Request flow

The application uses the standard Codex **provider configuration** to route requests through a local HTTP endpoint, keeping track of the original upstream address, authentication method, and additional request headers.

```mermaid
flowchart LR
    A["Codex"] <-->|HTTP / SSE| B["Stable local endpoint"]
    B <--> C["Request analysis and optimization"]
    C <--> D["Original upstream service"]
    C --> E[("Local request records")]
    E --> F["Desktop / browser interface"]
```

The stable endpoint receives requests. The background service analyzes them, applies enabled optimizations, and forwards them to the original upstream service. Responses stream back to Codex, while the interface reads local records to display request details.

### Compression and protection

| Mechanism | Implementation |
| :--- | :--- |
| **Lossless transport compression** | Compress the upload body with zstd. Try recompressing existing zstd requests and compare sizes before deciding whether to replace the body. |
| **Predictive protection** | Assess risk from the encoded request size and raise the input token count in a successful response to prompt native Codex compaction. Actual upstream usage is saved separately for statistics. |
| **Over-limit protection** | Identify a suitable history prefix, generate a handover summary through a Responses request, rebuild the request, and check the upload size. |
| **Result reuse** | Cache history-prefix fingerprints and processing results by session. Reuse matching results and reassess when history or relevant settings change. |
| **Capacity retries** | Detect explicit capacity errors in HTTP or streaming responses and retry with the same model and request according to the settings. |

### Image handling and separate analysis

Image handling has two parts: saving local attachments and providing a separate image analysis tool.

When enabled, the relay saves embedded images as local attachments and uses their paths in the main request sent upstream. The image analysis tool connects through MCP, analyzes images in a separate session, and returns text results to the main session.

### Usage and request records

Token usage comes from upstream responses and existing local Codex records; context-window limits are read from native records. When data cannot be matched or has not yet been written, the interface shows an unavailable or pending state.

Request records and processing results are stored in **SQLite**. Sensitive authentication headers are forwarded without saving their values in captured records. Browsing and JSON downloads use sanitized copies.

### Desktop and background service

| Technology | Purpose |
| :--- | :--- |
| **Python** | Local HTTP service, request processing, and configuration management |
| **zstandard** | zstd compression and decompression of request bodies |
| **SQLite** | Request records and cached history-processing results |
| **HTML / CSS / JavaScript** | Three-column interface, request analysis, and Settings |
| **pywebview / WebView2** | Windows desktop window |
| **PyInstaller** | Single-file EXE builds |

The desktop window and relay service run in separate processes and communicate through a local API. During an update, the application starts and verifies the new background service before routing new requests to it. The old service exits after finishing its existing requests.

### Building and releases

The shared build entry point is:

```powershell
python -m scripts.build_inspector --mode release
```

The build freezes a complete source snapshot, records the Git commit and file fingerprints, and verifies embedded EXE resources and frontend content. Official releases use a clean, integrated `main` branch. If the source changes during a build, the build is discarded and must be repeated.

See **[development and builds (Chinese)](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/docs/development.md)** for environment setup, previews, tests, and the build workflow.

---

## 📄 License

This is an independent third-party tool licensed under the **[MIT License](https://github.com/qzz1008-glitch/codex-request-optimizer/blob/main/LICENSE)**.

Third-party component notices and licenses are included in the release file `THIRD_PARTY_NOTICES.txt`.
