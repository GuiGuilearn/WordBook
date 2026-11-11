# 给编程 AI 的工作说明

你正在开发本目录的 Windows 小程序 **WordBook**。先完整阅读 `开发任务书.md`；它是功能、默认值和验收标准的唯一依据。本文件规定执行方式，不能擅自删减任务书中的核心流程。

## 目标

交付可以在这台 Windows 电脑上运行的 Python 桌面程序及其源代码。用户应能复制或拖入英文，使用自己在设置页填写的 DeepSeek API 密钥获取翻译，自动存入本机词库，复习，并导出最近 7/30 天的 Markdown。必须实测真实 GUI 和打包产物；只有代码通过静态检查不算完成。

## 执行顺序

1. 先检查本目录、可用 Python 与 Windows 环境，确认任务书中涉及的 API、模型名和依赖仍可用。建立项目独立 `.venv`；不要改动 Codex 自带运行时或安装全局包。
2. 先实现最小完整主链路：浮窗/托盘 → 输入 → 本地原文入库 → 后台翻译 → 卡片展示 → 重启后读取。随后补齐贴边行为、快捷键、重复记录、词库、复习、导出、备份和设置。
3. 为数据库与规则写必要的测试，尤其验证重复记录、跨日复习、7/30 天边界和备份恢复；API 使用模拟响应验证成功与失败。测试不能代替真实界面操作。
4. 构建 PyInstaller 文件夹式 Windows 程序，并真正启动产物，检查窗口、托盘、输入、数据持久化和关闭重开。若不能验证某项，明确写成“未验证”并说明原因。
5. 最后更新面向用户的 README：如何启动、如何获取并填写密钥、费用与隐私提示、数据存放位置、如何备份/恢复、如何使用快捷键与复习。

## 不可遗漏的约束

- 只在用户主动提交文本时调用 API；不要后台监听并上传每次剪贴板变化。
- API 密钥只放 Windows 凭据管理器，不写入代码、配置文件、SQLite、备份或日志。不要让用户在聊天中粘贴密钥。
- 提交后先持久化原文；网络失败时保留“待翻译”，可手动重试。已有解析重新生成失败时保留旧版。
- 每次遇到同一文本都要留下事件，供 7/30 天导出统计；同一文本的卡片合并。时间在数据库中用 UTC，复习和导出按本地日期。
- API 请求放后台，不能卡住浮窗。模型输出必须校验；不要把不完整或不合法的解析当作成功结果。
- 首版复习提示只在展开浮窗时出现，不发系统通知。模型一次返回详细解析，展开详情时不再次调用 API。
- 默认开机启动只对交付后的程序生效；源码调试不能把开发脚本登记为开机启动。
- 使用屏幕可用区域处理贴边、多显示器与分辨率变化，确保浮窗始终可找回。

## 遇到不确定项时

任务书已经决定的行为直接照做。若某个官方接口、模型名或系统行为发生变化，先查官方文档，采用保持产品目标的最小调整，并在交付说明中写明。涉及用户个人偏好、费用承诺或无法从环境确认的规则时，向用户提出具体问题；不要瞎猜或在成品中留占位功能。

## 交付清单

- 可运行的源代码、依赖清单、构建脚本和 README。
- 可独立启动的 Windows 程序包。
- 必要的自动化测试及其结果。
- 真实 GUI 与打包程序的运行证据；API 是否用真实密钥调用过，必须单独说明。
- 对照 `开发任务书.md` 第 5 节逐项报告完成、未完成和未验证项。

## 本地精简交付版交接说明

> 适用目录：`D:\1_study\codex\WordBook`。该目录是发布/运行目录，不是源码工作目录。
> 如果当前目录是通过 GitHub 克隆得到的源码目录，上方开发说明仍然适用；本节记录
> 2026-11-11 的本地交接状态和重新开发入口。

### 当前目录约定

- 本地只保留：`AGENTS.md`、`dist\WordBook\` 运行目录、`dist\WordBook-Windows-x64.zip`。
- 本目录不再保留 `.git`、源代码、虚拟环境、测试缓存或验收证据。
- `dist\WordBook\WordBook.exe` 必须和同目录的 `_internal` 一起使用；不要单独复制 EXE。
- 不要运行 `build\WordBook\WordBook.exe`。历史上该路径缺少 `_internal`，会报
  `Failed to load Python DLL`。可运行入口只有 `dist\WordBook\WordBook.exe`。
- `.pytest_cache` 若因 Windows ACL 权限残留，可忽略；它不是程序依赖，也不应重新加入源码仓库。

### 源码和版本

- GitHub：`https://github.com/GuiGuilearn/WordBook`
- 远端：`git@github.com:GuiGuilearn/WordBook.git`
- 分支：`main`
- 源码基线提交：`49e924aac7539b9b87e89eb49030952c184f9a35`
- 后续以 GitHub `main` 最新提交为准；源码仓库包含源代码、测试、`开发任务书.md`、
  `README.md`、验收报告和构建脚本。
- 仓库是公开仓库。提交前必须确认没有 API 密钥、用户词库、备份、个人数据和验收证据。

### 当前成品

- 可运行目录：`D:\1_study\codex\WordBook\dist\WordBook\`
- 恢复 ZIP：`D:\1_study\codex\WordBook\dist\WordBook-Windows-x64.zip`
- `WordBook.exe` SHA-256：
  `7B3509E5CE92EE1560C759A9C3DE4DBCB423F2F57FA16D18E81275C2C31AD2F3`
- ZIP SHA-256：
  `7C938D95B49F25F363F977EB70CD63D16FE4215D0A26B3B895B0EFF90849599E`
- 上述哈希是本地精简交付时的成品身份；替换成品后必须重新生成并核对哈希。

### 运行和用户数据

- 启动：双击 `dist\WordBook\WordBook.exe`，保留整个 `dist\WordBook` 文件夹。
- 用户数据库和备份位于 `%LOCALAPPDATA%\WordBook\WordBook`；不要在升级、清理或重建时删除。
- API 密钥只保存在 Windows 凭据管理器，不写入代码、配置、SQLite、备份或日志。
- 替换成品前先关闭正在运行的 WordBook。若 EXE 被 Windows Defender 隔离，先检查
  Windows 安全中心的保护历史，从 ZIP 恢复匹配的 EXE，再核对哈希；不要构建一个缺少
  `_internal` 的替代启动文件。
- 自动化或冒烟测试必须设置独立的 `WORDBOOK_TEST_DATA_DIR`，不要使用真实用户数据目录。

### 修改源码和重新构建

不要在精简发布目录里直接改代码。需要修改时，从 GitHub 克隆到单独的源码目录，例如
`D:\1_study\codex\WordBook-src`：

```powershell
git clone git@github.com:GuiGuilearn/WordBook.git D:\1_study\codex\WordBook-src
Set-Location D:\1_study\codex\WordBook-src
py -3.10 -m venv .venv310
.\.venv310\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv310\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .tmp\pytest-run
.\build.ps1
```

本机已知可用的 Python 3.10 路径为
`C:\Users\GuiGui\AppData\Local\Programs\Python\Python310\python.exe`。
`build.ps1` 目前要求项目内存在 `.venv310`，默认把程序输出到 `dist\WordBook\`，并生成
`dist\WordBook-Windows-x64.zip`。构建后至少需要：

1. 用独立测试数据目录运行源码版 GUI，确认窗口、托盘、输入、持久化和关闭重开。
2. 启动打包后的 `dist\WordBook\WordBook.exe`，验证真实窗口和打包运行。
3. 检查 `_internal\python310.dll` 存在，并更新成品哈希。

### 证据边界

- 之前的源码测试、真实 GUI 冒烟和打包冒烟证据已经按要求从精简目录移除。
- 移除证据不等于本轮重新验证；如果用户要求确认当前成品，必须重新执行真实 GUI 和打包验证，
  不能只依据静态文件或历史报告声称“已验证”。
- API 历史验证是模拟响应还是真实密钥调用，必须以当时的报告为准；不要在交接文档中改写结论。
