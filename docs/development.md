# 开发与构建

所有命令均在仓库根目录执行。应用代码位于 `packet_inspector/`，开发工具位于 `scripts/`，测试及兼容性样例位于 `tests/`。

## 前端开发与热更新

前端迭代使用开发版 EXE。HTML、CSS、JavaScript 从开发资源目录读取，保存后约 2 秒自动刷新，无需重新打包；支持刷新按钮、F5 和 Ctrl+R。构建及打开方式见下方“从源码运行和构建”。不再提供独立网页预览服务或启动脚本。

日常流程是修改前端、在开发版窗口确认交互，最后再打包完整 EXE。Python 后台及原生窗口代码修改需要重新构建；迭代期间保持正在使用的中转服务运行，未经用户明确要求不得停止或覆盖。

桌面窗口的尺寸、三栏宽度及最小尺寸以 `packet_inspector/ui/style.css` 为准。原生窗口尺寸修改需要重新构建并打开；前端样式修改可在开发版实时生效。桌面顶栏整合窗口按钮，模式药丸居中。

设置中的 zstd 压缩等级范围为 1–17，首次及旧配置缺省为 3，升级保留已保存的等级。保存后作用于新请求，也会无损重压缩已有 zstd 的 Responses 正文，仅在新包更小时替换；仅分析、gzip 及未经验证的端点仍原样转发。图片处理、超限兜底和其摘要子请求使用同一等级。各等级的大小估算相对原始未压缩 JSON，耗时占比为本地压缩中位数 / 历史整次请求中位数；基于 200 个本机脱敏副本、每级三轮测试，未计上传变小的收益，实际随内容、设备和网络变化。

自动等级默认关闭，仅在请求大小保护开启时允许启用，关闭父开关会同时关闭自动等级。开启后保留手动等级但禁用其输入，并显示旁注；自动模式仅在“优化”传输模式生效。首先检查当前正文的未压缩大小，严格小于用户预算的 90% 时跳过额外压缩，优先于历史档位；已有 zstd 按解压后的大小判断，跳过时保留原包及编码。其余请求以同一上游、同一会话上一条主请求的“压缩包”大小（摘要处理前）判断等级：达到预算的 90% 用 17 级，降到 80% 用 1 级，中间保持当前档位。未知会话默认 1 级；最快档不重压已有 zstd，当前已超限的 zstd 直接尝试 17 级。每个正文最多尝试一个等级，保留更小的包，随后继续执行原有预测及超限保护。图片改写和摘要子请求也先检查重组后的未压缩大小，小于预算的 90% 时不压缩，否则沿用本次选定等级；辅助请求不更新主请求的判定。历史判定缓存在内存，最多 512 个会话、有效期 1 小时，不新增缓存文件；重启后可从最近的已采集记录恢复判定。请求记录保存实际采用的等级，未重新压缩的原生 zstd 不推测等级。

图片解析支持原生、禁用、优化三种模式。原生卸载本程序安装的独立工具并启用标准 `tools.view_image`；禁用关闭原生工具及已有的独立图片工具；优化启用独立解析和主请求图片路径替换。EXE 将独立 PowerShell stdio MCP 工具安装到 Codex 配置目录的 `tools/codex-inspector-image-tool`，由 Codex 启动和管理，识图仍通过独立的临时 Codex CLI 会话完成。EXE 只管理安装、卸载、升级和注册；中转没有图片工具运行服务或 Hook。子会话使用标准 provider 配置，模型请求仍可能经过用户配置的中转。安装和配置一起回滚，卸载只删除安装清单所属的文件，保留用户其他文件；已有本程序 HTTP 注册会迁移到 stdio。模式切换修改标准配置，工具入口变化后需要重新加载 ChatGPT App。旧开关关闭迁移为原生，开启迁移为优化；首次使用仍默认原生。旧后台保持原生与优化切换，禁用选项在支持新模式的后台上可用。

右侧的“Token 窗口占用”优先使用预测保护前保存的真实上游用量，其他请求只读本机 Codex 历史中的单次 `token_count`；窗口上限仍读取原生记录。百分比按输入 token / 窗口上限计算，输出 token 单独列出；缺失或无法匹配时显示“—”。

大历史的 Token 查询在限定时间内连续读取多个有界片段，未读完时快速续读，不再等待两秒的页面刷新周期。已完成请求的确认结果缓存在页面内存中，最多 256 条、有效期 5 分钟；请求状态或耗时变化时失效，关闭页面后释放，不新增磁盘数据。切换请求会取消旧查询，旧响应不会覆盖当前详情。原生用量尚未写入时仍显示等待状态。

左侧会话栏显示名称、完整 session_id、最近更新时间和最近 Token 窗口占用，并按更新时间排序。会话名称和更新时间只读 Codex 的本机数据库，名称缺失时补读会话索引；Token 只读历史末尾的有限范围，原生压缩后的估算带 `≈` 标记。会话摘要约每 5 秒刷新。临时任务依据原生子任务关系或请求元信息中的明确父 ID 归入父会话，普通有名称的独立分支保留自己的行；名称或归属未确认的请求统一显示在“其他”。父会话行筛选包含所归属任务的请求，Token 保留父会话自身的记录。请求详情分别显示“请求与响应”“会话信息”和有图片时的“图片信息”，保留原始 session_id 和图片路径，不修改原生历史。

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

开发版默认输出到 `.local/dev-builds`，只生成开发 EXE 和校验文件；正式发布默认使用 `--mode release`，用户发布文件集中在 `publish/`，不包含开发 EXE。同一数据目录只允许一个桌面窗口，开发版、发布版及不同构建之间也互斥；重复启动或从托盘打开会恢复已有窗口。点击窗口“×”收到托盘，保留页面状态，中转继续运行；如果连接的是没有托盘的独立服务，则最小化到任务栏，避免窗口无法找回。停止整套程序请使用托盘中的“退出程序”；仍有请求时提示并保持运行，请求完成后可再次退出。退出保留 provider 配置，下次打开继续使用；退出期间本机中转不可用。

构建正式版 EXE、用户必要文件及内部验证产物：

```powershell
python -m scripts.build_inspector --output-dir release/local-build
python -m scripts.verify.verify_desktop_exe --release-dir release/local-build
python -m scripts.verify.verify_inspector_exe --release-dir release/local-build
python -m scripts.verify.verify_overflow_exe --release-dir release/local-build
python -m scripts.verify.verify_usage_exe --release-dir release/local-build
python -m scripts.verify.verify_upgrade_exe --release-dir release/local-build --desktop
python -m scripts.verify.verify_self_upgrade_exe --release-dir release/local-build
```

结果位于指定的全新目录。`publish/` 固定只包含 `CodexPacketInspector.exe`、`README.md`、`LICENSE`、`THIRD_PARTY_NOTICES.txt`，GitHub Release 也只上传这四个文件。第三方许可证全文汇总在 notices 文件中，并内嵌在 EXE 中。主程序同时承载界面、后台、固定入口和安装升级功能；不再生成独立升级包、旧文件名别名、源码归档或便携 ZIP。仅供验证的开发版 EXE、`BUILD-SOURCE.json` 和校验文件留在输出根目录，不进入用户发布文件。

更新功能直接选择正式版 EXE，并兼容读取历史 Release 的旧别名。EXE 兼容性验证使用已发布 v0.6.14 更新器的原始代码，针对实际发布的四个文件检查下载、升级及模式保留；更早的只识别旧别名的客户端需手动运行正式主程序。开发版的设置页使用同一更新来源，升级后继续保持开发模式。已有发布目录不覆盖。源码快照仍覆盖完整产品、测试、文档及构建工具，排除本机配置、请求数据库、认证和调查记录。GitHub Actions 在 Windows 上运行测试、构建与 EXE 升级验证；推送与源码版本一致的 `vX.Y.Z` 标签会在全部验证通过后发布 GitHub Release。发布前再次校验源码、每个公开文件的哈希、公开目录精确白名单及正式 EXE 与已验证程序一致，阻止开发产物混入。

测试只向临时本机后端发送合成请求，不使用真实认证，不调用云端模型。安装入口需要正式 EXE 和默认数据目录，测试实例不能更改登录任务。

## 固定分支与工作树流程

采用 GitHub Flow，规则固定在仓库根目录 `AGENTS.md`，所有 agent 都遵守。`main` 是唯一长期集成和交付分支；功能、修复、整理分别使用 `feat/<任务>`、`fix/<任务>`、`chore/<任务>` 短期分支。没有独立的长期发布工作树或 `develop` 分支，版本通过 `vX.Y.Z` 标签标记。

默认保留一个主工作树。单任务从最新 `main` 开任务分支，完成后提交、验证、合入，再删除分支并让主目录回到 `main`：

```powershell
git fetch origin
git switch main
git pull --ff-only
git switch -c feat/task-name
# 完成修改和相关测试后提交
git add <本任务文件>
git commit -m "Describe the completed change"
# 若 main 已有新提交，先在任务分支合并最新 main 并重新验证
git switch main
git merge --ff-only feat/task-name
git branch -d feat/task-name
```

多人或多个 agent 并行时，每个任务使用独立的临时工作树和分支，不能在另一个任务正在使用的目录切分支、提交或覆盖文件。完成后将整个任务分支合入主目录 `main`，清理分支及工作树；应用托管的工作树由所属会话先保存需要的忽略文件，再使用应用归档工具清理。禁止把一个长期发布工作树当作另一份产品源码。GitHub 协作使用 PR，CI 通过后合入 `main`；本地整合保持可快进合并，不强推 `main`。

## 所有 agent 共用的构建来源

交付和正式发布只允许从主工作树的干净 `main` 构建；CI 可从指向最新 `main` 的版本标签构建。完成的产品改动必须先合入 `main`，不能只留在其他分支或未提交状态。正式构建前后联网检查远端。

任务分支可以用 `--mode dev` 构建验证包，但必须包含最新本地 `main` 和已知 `origin/main`；验证包不作用户交付。其他尚未完成的任务分支不自动进入发布，也不阻止干净 `main` 构建。主目录存在未提交产品源码时，先整合这些改动，再从其他工作树构建。`main` 代表所有已整合改动的唯一来源。

```powershell
python -m scripts.build_inspector --mode dev --check-source
python -m scripts.build_inspector --mode dev
python -m scripts.build_inspector --mode release
```

构建持有整个仓库共享的锁，先复制完整源码快照，再从快照编译。正式与开发 EXE 使用同一份快照。每个 EXE 内嵌提交号、版本、源码 SHA-256 和逐文件指纹；编译后检查内嵌清单、四个前端文件和桌面窗口模块。构建期间如有 agent 修改、增加或删除源码，或提交发生变化，本次构建直接失败，不向交付目录复制产物。输出目录必须全新，禁止覆盖、复用旧包。

输出目录中的 `BUILD-SOURCE.json` 保存完整来源清单；桌面 `/api/status` 的 `ui_build` 返回当前 EXE 的版本、提交和源码指纹，`ui_version` 仍用于判断桌面与服务版本。开发版前端保留实时更新，因此其磁盘资源可晚于 EXE，`/__dev__/state` 返回当前页面资源指纹。托管开发资源目录按版本与源码指纹区分，同版本重打包也不会沿用旧构建缓存；明确指定的本地源码目录仍保留实时读取。

本仓库使用 `.githooks/pre-push` 检查 `main` 和 `v*` 发布标签：只允许发布已整合的当前主目录提交，拒绝非快进覆盖远端 `main` 和替换已发布标签，禁止把旧工作树提交作为新版本发布。首次在新克隆中配置共享钩子路径（绝对路径对所有关联工作树生效）：

```powershell
git config core.hooksPath "$((Get-Location).Path)/.githooks"
```

GitHub Actions 对 PR 运行回归测试；`main` 和版本标签还会构建并验证桌面窗口、两种运行模式和升级路径。发布前重新检查远端、当前源码清单、版本与 EXE 校验值；主分支在构建期间有更新时，旧构建不能发布。不得跳过这些校验或直接运行 PyInstaller。

本项目采用 MIT 许可证。运行时组件的许可证见 `THIRD_PARTY_NOTICES.txt`。发布包未做代码签名，Windows 是否提示未知发行者取决于本机策略。
