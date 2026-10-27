## 简体中文

适用于 Windows x64（64 位）。

本次更新压缩等级与效果估算、历史趋势浏览、响应概要及保护状态展示。

- **压缩等级与估算**：手动 zstd 等级扩展为 1–22，按原始请求小于 1.5 MB、达到 1.5 MB 两组展示压缩大小和本机耗时估算，并用颜色区分各组的相对效果。自动高压缩档调整为 16 级，保留已有会话的选档状态。
- **历史趋势浏览**：拖动时间滑动条或滚轮浏览保留的历史，Ctrl＋滚轮以光标为中心缩放，展示范围为 1–12 小时，刻度最细为 10 分钟。浏览历史时保持当前位置，回到最右端后继续跟随最新请求；聚焦机制图例可高亮对应节点。
- **趋势加载**：按当前时间范围读取轻量概要数据并增量刷新，先确认可浏览范围，再加载对应记录，减少不必要的正文读取。
- **响应与记录详情**：随请求保留用量、完成状态和耗时等响应概要；详情回收后仍可查看保留的统计对比。基本记录隐藏没有正文的标签页，“请求与响应”精简为模型、结果和总耗时，异常时显示已记录的原因。
- **保护状态与术语**：统一“重试保护”及请求大小相关名称，完善列表中的保护色条和趋势标记，精简左栏底部计数。已发送原生重试等待信息与实际重试记录继续按各自含义展示。
- **上游读取稳定性**：普通转发和超限摘要的读取空闲超时统一为 300 秒，与 Codex 原生默认值一致；持续收到数据时不限制整轮生成总时长。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；正常升级时，新请求交给新版服务，处理中请求由原服务继续完成。

## English

For Windows x64 (64-bit).

This release updates compression levels and estimates, historical trend navigation, response summaries, and protection indicators.

- **Compression levels and estimates**: expand manual zstd levels to 1–22. Show compressed-size and local processing-time estimates separately for original requests below 1.5 MB and those at or above 1.5 MB, with colors indicating relative results within each group. Set automatic high compression to level 16 while preserving existing per-session phase state.
- **Historical trend navigation**: browse retained history with the time slider or mouse wheel. Use Ctrl + wheel to zoom around the cursor, with a 1–12-hour viewing range and ticks down to 10-minute intervals. Keep the current position while browsing history, resume following new requests at the right edge, and focus a mechanism legend item to highlight matching nodes.
- **Trend loading**: load lightweight summaries for the current time range and refresh incrementally. Resolve the available history range before loading its records to avoid unnecessary request-body reads.
- **Response and record details**: retain response summaries such as usage, completion status, and timing with each request. Keep statistical comparisons available after detailed content is reclaimed. Hide the body tab for basic records, and simplify request/response information to model, result, and total time, with the recorded reason shown for errors.
- **Protection indicators and terminology**: standardize retry-protection and request-size labels, refine protection bars in the request list and trend markers, and simplify sidebar footer counts. Sent native retry wait information and historical actual retries retain their distinct meanings.
- **Upstream read stability**: use a 300-second read-idle timeout for both regular forwarding and overflow summaries, matching the native Codex default. Continuously arriving data does not impose a deadline on the entire generation.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. During a normal upgrade, the new service handles new requests while the previous service finishes requests already in progress.
