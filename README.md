# Codex 请求优化器

**Codex Request Optimizer** · Windows 本地请求查看与优化工具

按会话查看 Codex 请求，通过 Zstd 压缩和上下文保护控制上传体积，并支持平滑升级。

**[下载最新版](https://github.com/qzz1008-glitch/codex-request-optimizer/releases/latest)** · [使用指南](docs/usage.md) · [开发与构建](docs/development.md)

## 能做什么

- **查看请求**：按会话浏览请求、上传大小、输入 Token 和请求内容。
- **压缩传输**：Zstd 无损压缩，保留完整消息、工具返回和图片。
- **控制大小**：接近上传上限时触发预测保护，超限时压缩旧上下文；统计保留真实 Token 用量。
- **平滑升级**：固定入口接替后台，在途请求继续完成，沿用历史数据和运行模式。

## 开始使用

1. 从 [Releases](https://github.com/qzz1008-glitch/codex-request-optimizer/releases/latest) 下载并打开主程序。
2. 在设置中接入 Codex，按提示重启 ChatGPT App。
3. 后续通过设置中的“检查更新”完成升级。

Release 只提供一个完整主程序。开发版升级后继续使用开发模式，正式版升级后继续使用正式模式。

## 项目结构

```text
packet_inspector/      应用、中转、桌面窗口与前端
scripts/               开发预览与构建入口
  verify/              打包程序与升级验证
  windows/             Windows 安装、更新及图标工具
tests/                 自动化测试
  fixtures/            旧版兼容性样例
docs/                  使用、开发和第三方声明
.github/               持续集成与发布配置
```

## 开发入口

在仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -p "test_*.py" -q
python -m scripts.dev_inspector_ui --open
```

构建 EXE、验证升级及发布流程见[开发与构建](docs/development.md)。

## 许可

[MIT](LICENSE) · [第三方声明](docs/THIRD_PARTY_NOTICES.txt)

本项目是独立的第三方工具，不属于 OpenAI 官方产品。
