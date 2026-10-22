# 开发与构建

所有命令均在仓库根目录执行。应用代码位于 `packet_inspector/`，开发工具位于 `scripts/`，测试及兼容性样例位于 `tests/`。

## 前端开发预览

在源码目录运行 `python -m scripts.dev_inspector_ui --open`，浏览 <http://127.0.0.1:47845/>。HTML、CSS、JavaScript 每次从源码读取，保存后约 2 秒自动刷新，无需先打包 EXE。预览读取 47844 上已有中转的真实记录和状态；可保存独立入口端口及使用版本检查、升级，暂停采集、保存中转设置、接入和恢复出口等写入仍禁用。预览启动、重启不影响中转；只有明确选择“立即升级”或启用自动升级时才调用升级程序。可用 `--port` 和 `--relay-port` 指定端口。

日常流程是先修改前端、在预览页确认交互，再在确认后打包新 EXE。Python 预览服务代码变动时只需重启预览进程，前端文件变动会自动刷新。预览进程可随时重启，无需再次确认；正在使用的中转服务保持运行。

HTML 预览按 1200 × 760 的固定画布居中显示，不随浏览器全屏拉伸；页面较小时可滚动查看。窗口最小宽度为 1120 像素，此时三栏宽度为 260 / 380 / 480；左栏固定 260、右栏固定 480，拉宽窗口时中栏扩展。桌面窗口从同一份 CSS 尺寸配置读取默认宽高与最小尺寸，升级重新打开界面后生效。

右侧的“Token 窗口占用”优先使用预测保护前保存的真实上游用量，其他请求只读本机 Codex 历史中的单次 `token_count`；窗口上限仍读取原生记录。百分比按输入 token / 窗口上限计算，输出 token 单独列出；缺失或无法匹配时显示“—”。

大历史的 Token 查询在限定时间内连续读取多个有界片段，未读完时快速续读，不再等待两秒的页面刷新周期。已完成请求的确认结果缓存在页面内存中，最多 256 条、有效期 5 分钟；请求状态或耗时变化时失效，关闭页面后释放，不新增磁盘数据。切换请求会取消旧查询，旧响应不会覆盖当前详情。原生用量尚未写入时仍显示等待状态。

左侧会话栏显示名称、完整 session_id、最近更新时间、最新请求的最终上传大小和最近 token 窗口占用，并按更新时间排序。会话名称和更新时间只读 Codex 的本机数据库，名称缺失时补读会话索引；token 只读历史末尾的有限范围，原生压缩后的估算带 `≈` 标记。会话摘要约每 5 秒刷新，缺失字段显示“—”；旧预览服务仍可显示请求时间与上传大小，加载新版预览服务后可读取名称和 token。

## 从源码运行和构建

需要 Windows x64、Python 3.12+。建议在虚拟环境中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -p "test_*.py" -q
python -m packet_inspector
```

首次运行采用构建的默认模式，后续升级保留已安装的开发版或正式版模式，设置页没有模式切换开关：

- 开发版：`CodexPacketInspector-Dev.exe`，标题标注“开发版”，读取当前项目的 `packet_inspector/ui`。支持刷新按钮、F5、Ctrl+R 和保存后自动刷新；前端修改无需重新构建，Python 代码修改需重新构建。
- 发布版：`CodexPacketInspector.exe`，使用打包页面，隐藏刷新按钮，不启用开发快捷键、文件轮询和本机资源覆盖。

构建并打开开发版：

```powershell
python -m scripts.build_inspector --mode dev --output-dir .local/desktop-dev-build
Start-Process .local/desktop-dev-build/CodexPacketInspector-Dev.exe
```

开发版默认输出到 `.local/dev-builds`，只生成开发 EXE 和校验文件；正式发布默认使用 `--mode release`，生成的安装包和发布归档不包含开发 EXE。同一数据目录只允许一个桌面窗口，开发版、发布版及不同构建之间也互斥；重复启动或从托盘打开会恢复已有窗口。点击窗口“×”收到托盘，保留页面状态，中转继续运行；如果连接的是没有托盘的独立服务，则最小化到任务栏，避免窗口无法找回。停止整套程序请使用托盘中的“退出程序”；仍有请求时提示并保持运行，请求完成后可再次退出。退出保留 provider 配置，下次打开继续使用；退出期间本机中转不可用。单独使用浏览器预览仍可运行 `python -m scripts.dev_inspector_ui --open`。

构建单文件 EXE 和干净的源码包：

```powershell
python -m scripts.build_inspector --output-dir release/local-build
python -m scripts.verify.verify_inspector_exe --release-dir release/local-build
python -m scripts.verify.verify_overflow_exe --release-dir release/local-build
python -m scripts.verify.verify_usage_exe --release-dir release/local-build
python -m scripts.verify.verify_upgrade_exe --release-dir release/local-build --desktop
python -m scripts.verify.verify_self_upgrade_exe --release-dir release/local-build
```

结果位于指定的全新目录，包含正式主程序、仅供本地验证的开发程序和构建验证资料。GitHub Release 仅上传一个完整主程序，下载名为 `CodexRequestOptimizer-Upgrade.exe`，与本地构建的 `CodexPacketInspector.exe` 内容相同；保留该文件名使旧版设置可直接升级。不发布开发版、独立升级器、脚本、源码归档或校验文件。主程序同时承载界面、后台、固定入口和安装升级功能。开发版的设置页使用同一更新来源，升级后继续保持开发模式。已有发布目录不覆盖。源码包使用文件白名单，不包含本机配置、请求数据库、认证、调查记录或原 App 资源。GitHub Actions 在 Windows 上运行测试、构建与 EXE 升级验证；推送与源码版本一致的 `vX.Y.Z` 标签会在全部验证通过后发布 GitHub Release。

测试只向临时本机后端发送合成请求，不使用真实认证，不调用云端模型。安装入口需要正式 EXE 和默认数据目录，测试实例不能更改登录任务。

本项目采用 MIT 许可证。运行时组件的许可证见 `THIRD_PARTY_NOTICES.txt`。发布包未做代码签名，Windows 是否提示未知发行者取决于本机策略。
