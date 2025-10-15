# Echo Client

一个为 Echo-live/OBS 工作流打造的命令行控制台，专注于让无声系虚拟主播和需要批量发送字幕的创作者高效控制弹幕展示。它提供可视化的 CLI 体验、本地配置持久化、丰富的富文本格式，并支持打包为单文件可执行程序。

> ⚠️ 项目仍在积极开发中，行为可能随版本演化。本文档描述的是 `master` 分支当前实现。

## ✨ 主要特性

- **即开即用的本地 WebSocket 服务器**：在本机监听 Echo-live 广播端口，与 OBS 中的 Echo-live 无缝对接。
- **交互式命令行体验**：基于 `prompt-toolkit` 的彩色终端，内置快捷键和命令提示。
- **富文本格式与快速标记**：支持 Markdown 强调语法与 `@` 前缀快捷码，快速叠加粗体、斜体、颜色、字号、类名等效果。
- **Typewriting 与自动停顿**：按需生成打字机效果与自动插入停顿帧，让字幕播放更自然。
- **可配置的消息修饰**：可自动为文本添加引号、括号，并为用户名套上【】以突出显示。
- **批量脚本执行**：通过 `/source` 命令导入 `message_sample.txt` 等脚本文件，实现自动播报。
- **跨平台打包**：内置 PyInstaller spec，可将工具封装为单文件或单目录可执行程序。

## 🚀 快速开始

1. 在 OBS 中安装并配置最新的 Echo-live。
2. 打开 Echo-live 的 `config.js`，设置 WebSocket：

   ```js
   websocket_enable: true,
   websocket_url: 'ws://127.0.0.1:3000'
   ```

   - 若 Echo-live 与本程序不在同一台设备，`websocket_url` 请改为服务器的 IP，且在 echo-client 的配置中把 `host` 改为 `0.0.0.0`。
3. 安装并运行 echo-client：

   ```powershell
   pip install echo-client
   echo-client
   ```

   或在源码仓库：

   ```powershell
   poetry install
   poetry run echo-client
   ```

首次启动会在工作目录生成 `config.yaml`。终端会打印监听地址、配置路径等提示。

## 🔌 与 Echo-live 对接

- Echo-live 的 WebSocket 客户端连接到 `ws://<host>:<port>`（默认 `127.0.0.1:3000`）。
- 建议在 OBS 中刷新浏览器源以触发连接。
- 连接后，终端会显示客户端 ID、显示名称、心跳次数、实时展示状态等事件。

## ⚙️ 配置文件

配置保存于可执行文件或脚本所在目录的 `config.yaml`，字段说明如下：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `command_prefix` | `str` | `/` | 命令前缀。若要发送以 `/` 开头的消息，可输入 `//文本` 。|
| `username` | `str` | `Someone` | 推送给 Echo-live 的默认用户名，可交互命令 `/ren` 修改。|
| `host` | `str` | `127.0.0.1` | WebSocket 监听地址，跨设备使用请改为 `0.0.0.0`。|
| `port` | `int` | `3000` | WebSocket 监听端口。|
| `typewriting` | `bool` | `true` | 是否启用打字机同步。`/typewrite`（或 `/tt`）可切换。|
| `typewriting_scheme` | `str` | `pinyin` | 打字机模式，支持 `pinyin`（拼音）与 `zhuyin`（注音），`/scheme`（或 `/tts`）可切换。|
| `autopause` | `bool` | `false` | 自动插入停顿标记。`/autopause`（或 `/ta`）可切换。|
| `autopausestr` | `str` | `,，.。;；:：!！` | 触发停顿的字符集合。|
| `autopausetime` | `int` | `10` | 停顿时长单位，取决于打印速度。|
| `print_speed` | `int` | `10` | 默认打印速度（毫秒/字符），`/speed <value>`（或 `/ps`）可调整。|
| `auto_quotes` | `bool` | `true` | 是否自动为每条消息添加一对双引号，`/quotes`（或 `/tq`）可切换。|
| `auto_parentheses` | `bool` | `false` | 是否自动用圆括号包裹消息，可用 `/paren` 切换或 `/paren once` 仅对下一条生效。|
| `username_brackets` | `bool` | `false` | 是否使用 `【】` 包裹用户名，`/brackets`（或 `/ub`）可切换。|

每次通过命令修改都会即时落盘。手动编辑文件后无需重启即可生效（下一条消息时加载）。

## ⌨️ 命令与快捷键

### 控制台命令

命令以 `command_prefix` 开头，可使用别名简写：

| 命令 | 别名 | 描述 |
| --- | --- | --- |
| `/help [command]` | `/h`, `/?` | 显示命令列表或查看某个命令的详细说明。|
| `/quit` | `/q`, `/exit` | 关闭服务器并退出程序。|
| `/name <name>` | `/ren` | 更新默认显示名称并保存配置。|
| `/speed <ms>` | `/ps` | 设置默认打印速度（毫秒/字符）。|
| `/typewrite` | `/tt` | 切换 Typewriting 效果。|
| `/scheme` | `/tts` | 在拼音与注音模式之间切换 Typewriting。|
| `/autopause` | `/ta` | 切换自动停顿。|
| `/quotes` | `/tq` | 切换是否自动为消息添加双引号。|
| `/paren [once|on|off]` | `/tp` | 无参时切换圆括号包装；`once` 仅让下一条消息生效；`on/off` 显式设置。|
| `/brackets` | `/ub`, `/tub` | 切换是否用 `【】` 包裹用户名。|
| `/source <file>` | `/src`, `/load` | 按行执行脚本文件中的指令。|

> 想发送以 `/` 开头的纯文本，可输入 `//这是内容`，程序会自动转换。

### 输入快捷键

- `Ctrl+B` / `Ctrl+I` / `Ctrl+U` / `Ctrl+D`：插入 `@b`/`@i`/`@u`/`@s` 快速格式码。
- `Ctrl+↑` / `Ctrl+↓`：插入 `@+` / `@-` 调整字号。
- `Ctrl+Space`：插入 `@r` 重置临时样式。

## 📝 消息格式

echo-client 同时支持两套叠加格式：

1. **Markdown**：
   - `**文本**` 或 `__文本__` → 粗体
   - `*文本*` 或 `_文本_` → 斜体
   - `` `代码` `` → 代码风格
2. **快速格式化（Fast Formatting）**：

   | 片段 | 效果 |
   | --- | --- |
   | `@b` / `@i` / `@u` / `@s` | 粗体 / 斜体 / 下划线 / 删除线 |
   | `@[color]` | 设置颜色，例如 `@[#66ccff]` |
   | `@+` / `@-` | 放大 / 缩小字号（多次叠加） |
   | `@r` | 恢复默认样式 |
   | `@{emoji}` | 插入表情或图片占位符 ID |
   | `@<class>` | 添加 CSS 类，自动加上 `echo-text-` 前缀；`@<:class>` 则原样保留 |
   | `\@` | 输出字面量 `@`|

更完整的示例请查看仓库中的 `message_sample.txt`，可在程序内运行：

```text
/s message_sample.txt
```

> 默认会为消息自动添加一对双引号；可通过命令或配置关闭，或进一步叠加圆括号包装。

## 🤖 自动停顿与打字机

- **自动停顿（Autopause）**：开启后，程序会在 `autopausestr` 中的字符后插入一个 “pause” 事件，时长由 `autopausetime` 与打印速度共同决定。
- **Typewriting**：对中文使用 `jieba` 分词，对每段文字生成拼音，配合 Echo-live 的打字机效果；也可切换为注音模式，以 Bopomofo 输出。

两项能力都可随时通过命令切换，并立即作用于接下来的消息。

## 📦 打包可执行文件

仓库内置 `build.spec` 与 `echo-client.spec`。使用 PyInstaller 打包：

```powershell
poetry install  # 或 pip install 所需依赖
pip install pyinstaller
pyinstaller --onefile --name echo-client build.spec
# 或
pyinstaller echo-client.spec
```

构建完成的单文件位于 `dist/echo-client.exe`。可执行文件会在自身目录创建/更新 `config.yaml`，无需额外携带配置。

## 🛠 开发与贡献

```powershell
git clone https://github.com/xrh0905/echo-client.git
cd echo-client
poetry install
poetry run echo-client
```

- 代码格式建议使用 `ruff`/`pylint` 等工具（仓库默认提供 `pylint`）。
- 提交 PR 或 issue 前请说明使用场景，尤其是 Echo-live 与 echo-client 的版本信息。

## ❓ 常见问题

- **客户端无法连接**：确认 Echo-live 的 `websocket_url` 是否指向本机监听地址，并检查端口占用问题。
- **消息没有格式效果**：确保 Echo-live 版本支持传入的字段；`@` 快捷码与 Markdown 可叠加使用。
- **打字机太慢/太快**：通过 `/ps <毫秒>` 即时调整打印速度，或修改 `config.yaml` 后重启。
- **想要批量发送**：将指令写入文本文件后使用 `/source your_file.txt`（亦可使用别名 `/src`）。

欢迎通过 issue、讨论区或 PR 分享使用心得与改进建议！

