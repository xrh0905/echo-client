## 简介

Echo-client 是一个为无声系虚拟主播设计的命令行终端，不过也可以用于快速制作视频中的字幕

注意：本项目仍在活跃开发中，下方的所有信息可能会发生改变，请以代码为准。

## 安装

首先您需要正确地在 OBS 中安装 Echo-live。

然后定位到 `config.js` 的 websocket 这部分，将 `websocket_enable` 改为 `true`。

下方的 `websocket_url` 中请填写服务端的 ip 地址与端口号。（如果您在一台电脑上同时运行 OBS 和本程序则 ip 可以写 `127.0.0.1`）

```js
        // 启用 WebSocket
        // * 如果没人要求您这么做，请不要动它。
        // * 广播模式下启用 WebSocket 可连接至服务器以从第三方软件获取消息。
        // * 可从服务器接收的消息和广播消息一致，发送的消息须使用类似于 JSON.stringify 的方法序列化。
        // * 详见：https://sheep-realms.github.io/Echo-Live-Doc/dev/broadcast/
        websocket_enable: true,
        // WebSocket 连接地址
        // websocket_url: 'ws://192.168.1.12:3000', // iPad
        websocket_url: 'ws://127.0.0.1:3000',
```

如果您在两台设备上分别运行 OBS 和本程序，请修改配置文件中的 `host` 项为 `0.0.0.0`。

```sh
pip install echo-client
```

## 使用

您需要正确完成“安装”章节。

正确运行程序，控制台输出应该类似这样（有颜色）：

```
从 ./config.yaml 加载了配置
已经在 127.0.0.1:3000 监听 websocket 请求，等待 echo 客户端接入...
tips: 如果没有看到成功的连接请求，可以尝试刷新一下客户端
用户输入模块加载成功，您现在可以开始输入命令了，客户端连接后会自动执行！
请输入命令:
```

配置文件现在存储在程序工作目录下的 `config.yaml` 中，不再写入 AppData 或 `~/.config`。默认已经启用 Typewriting 模式，如需关闭可以手动修改配置或在交互界面使用 `/tt` 指令。

文本输入支持基础 Markdown 语法：使用 `**粗体**` 或 `__粗体__` 可以强调，`*斜体*` 或 `_斜体_` 可以倾斜显示，同样适用于命令里叠加的样式。

为了快速调整消息效果，可以使用“快速格式化代码”：

- `@b`、`@i`、`@u`、`@s` 分别表示粗体、斜体、下划线、删除线。
- `@[#66ccff]` 设置文本颜色，`@r` 清除临时格式。
- `@+` 与 `@-` 调整字号，`@<class>` 为文本添加类名，`@{id}` 插入表情或图片占位。

默认打印速度为 30ms/字符，可随时使用 `/speed 50`（或 `/printspeed`, `/ps`）命令调整，所有消息会自动携带 `printSpeed` 启动参数。

然后刷新 OBS 中的预览窗口，您应该可以看见 websocket 连接成功的提示。

命令应该怎么写呢？

请看项目根目录下的 message_sample.txt，这是一个包含了目前可以使用的所有格式的示例文件。

有关该文件的更多信息请点开查看。

## 配置

还没写。

## 打包成单文件可执行程序

项目包含一个 `build.spec`，可用于使用 PyInstaller 打包为单文件（one-folder）或单文件（onefile）可执行程序。步骤如下：

1. 安装依赖，可选择使用 Poetry：

        ```powershell
        poetry install
        ```

        或者直接通过 pip 安装运行时依赖（`pyproject.toml` 中的 `[tool.poetry.dependencies]` 列表）：

        ```powershell
        pip install websockets rich prompt-toolkit pypinyin pyyaml jieba markdown-it-py
        ```

2. 安装 PyInstaller：

        ```powershell
        pip install pyinstaller
        ```

3. 在项目根目录执行：

        ```powershell
        pyinstaller build.spec
        ```

打包完成后，可执行文件位于 `dist/echo-client/echo-client.exe`。配置文件 `config.yaml` 会自动复制到输出目录下，首次运行时会在该目录自动生成/更新。挂起的 `config.yaml` 允许在分发后的文件夹内直接调整运行参数。

