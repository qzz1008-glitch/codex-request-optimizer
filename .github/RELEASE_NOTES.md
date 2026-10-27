## 简体中文

适用于 Windows x64（64 位）。

本次更新自动压缩选档、原生容量重试、记录保留和图片工具兼容性。

- **自动压缩选档**：按上游和会话持久保存原包、快速压缩和高压缩阶段；高压缩保持至主请求回到小包范围。缺少档位时可从保留的请求元数据恢复，重启、暂停采集或清理正文不会丢失已有阶段。请求详情统一显示摘要替换前的压缩包大小、方式和等级。
- **原生容量重试**：容量错误的等待信息交给 Codex 原生处理，中转不再等待或重发请求。重试次数和等待时间均可设为 1–99，默认 15 次、30 秒；上游要求更久时遵循上游。请求详情中的“原生重试提示”表示已发送等待信息，不代表确认发生了实际重试。
- **记录优先保留**：默认 500 MB、2,000 条记录。容量不足时先回收旧请求详情，尽量保留基本信息、真实用量和趋势数据；基本信息自身超额时才删除旧记录。保存限额后立即回收，回收不会中断转发。左栏显示占用、请求数和详情数，大小单位统一为十进制 KB/MB。
- **趋势与保护状态**：趋势默认最近 1 小时，并显示设置中的请求大小上限参考线；完善压缩方式、等级和预测保护状态，减少不同处理阶段混用造成的误解。
- **图片工具兼容性**：图片工具配置与接入管理分离，保留用户连接配置；Codex 升级导致旧 CLI 路径失效时自动重新发现可用 CLI 并刷新工具注册。
- **文档**：同步中英文 README、使用指南和开发文档，说明当前配置、保留策略及使用方式。

发布文件仅包含 **CodexPacketInspector.exe**、**README.md**、**LICENSE** 和 **THIRD_PARTY_NOTICES.txt**。双击主程序即可安装或升级，也可在设置中检查更新。升级保留已有记录、设置及开发版／正式版模式；旧默认记录额度 536.870912 MB 一次性调整为 500 MB，自定义额度保留，旧详情条数限制停用。原生重试次数变更后需重新加载 Codex 配置；重启 ChatGPT/Codex 后可确保生效。

## English

For Windows x64 (64-bit).

This release updates automatic compression selection, native capacity retries, record retention, and image-tool compatibility.

- **Automatic compression selection**: persist original-body, fast-compression, and high-compression phases per upstream and session. High compression stays active until the main request returns to the small-body range. Missing phases can be recovered from retained request metadata; restarts, paused capture, or body cleanup do not discard existing phases. Request details consistently show the compressed size, encoding, and level before summary replacement.
- **Native capacity retries**: pass capacity-error wait information to native Codex handling instead of waiting or replaying requests in the relay. Retry count and wait time each support 1–99, defaulting to 15 retries and 30 seconds; longer upstream waits take precedence. “Native retry hint” in request details means wait information was sent, not that an actual retry was confirmed.
- **Prioritize request records**: default to 500 MB and 2,000 records. Reclaim older request details first to retain basic information, actual usage, and trend data; remove old records only when basic information itself exceeds the budget. Apply lower limits immediately without interrupting forwarding. The sidebar shows storage usage, request count, and detail count; sizes consistently use decimal KB/MB.
- **Trends and protection status**: default trends to the last hour and show the configured request-size limit as a reference line. Clarify encoding, compression levels, and predictive-protection states to avoid mixing different processing stages.
- **Image-tool compatibility**: separate image-tool configuration from connection management while preserving user connection settings. Rediscover an available CLI and refresh tool registration when a Codex update invalidates the old CLI path.
- **Documentation**: synchronize the Chinese and English READMEs, user guide, and development guide with current settings, retention policies, and usage.

Release assets contain only **CodexPacketInspector.exe**, **README.md**, **LICENSE**, and **THIRD_PARTY_NOTICES.txt**. Run the EXE to install or upgrade, or check for updates in Settings. Upgrades preserve records, settings, and the installed development or release mode. Migrate the old default record budget of 536.870912 MB to 500 MB once, preserve custom budgets, and retire the separate detail-count limit. Reload Codex configuration after changing the native retry count; restarting ChatGPT/Codex ensures it takes effect.
