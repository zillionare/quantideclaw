我想构建一个虚拟化的 openclaw, 安装在虚拟机之中，然后可以把这个容器或者虚拟机镜像发给客户。

现在要与你一起讨论方案。我将使用最小的 linux（debian 13） 来创建虚拟机，但需要能安装最轻量的桌面，以及 firefox(chrome 为尽力项)

要求构建基于 UTM （mac arm64）和 windows wsl2的虚拟机。

这个方案的最终目标是安装 openclaw 并进行一些定制。

你需要帮我选择满足条件的 linux,并且写两个脚本：

## 平台矩阵

| 维度                  | macOS ARM64                                                                            | Windows x64                                                                                  |
| --------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 分发形态              | UTM 虚拟机包                                                                           | WSL2 Debian 发行版导出包                                                                     |
| 基础系统              | Debian 13 arm64 rootfs                                                                 | Debian 13 x64 rootfs                                                                         |
| 创建方式              | 通过 UTM 创建或导入虚拟机                                                              | 通过 `wsl --install` / `wsl --import` 创建或导入发行版                                       |
| 首次启动 GUI 触发方式 | 通过 XFCE 自动登录和桌面自启动项触发                                                   | 通过 WSLg 启动 GUI；首次进入后的 GUI 触发方式可以不同于 UTM，不要求复用同一套登录/自启动机制 |
| 桌面要求              | 必须安装 XFCE，并能自动进入桌面                                                        | 必须安装 XFCE，并能在 WSLg 中启动                                                            |
| 浏览器要求            | Firefox 必装；若 Debian arm64 上没有可用的官方安装包或仓库，不阻塞交付，但必须记录原因 | Firefox必装， chrome 为尽力项。若官方仓库或安装包不可用，不阻塞交付，但必须记录原因          |
| 共用 provision 范围   | 镜像源、桌面、浏览器、OpenClaw、插件、初始化向导、资源打包、清理                       | 同左                                                                                         |
| 可接受差异            | 仅允许虚拟化创建步骤、首次启动 GUI 触发方式、导出/导入命令不同；其余配置目标应保持一致 | 同左                                                                                         |

## 系统定制脚本
1. 使用 Debian 13 发行版/rootfs。在 mac上使用 arm64, 在windows 上使用x64。
2. 在mac 上使用 utm，在 windows上使用 wsl2
3. 安装最小桌面（xfce?），firefox 和 opera(带免费 vpn 版本)
4. 切换 apt, pip, bun, npm 等源为中国大陆。
5. 安装openclaw，并且安装 weixin/qq 插件
6. 设置首次启动时自动加载的初始化程序（见`## 初始化脚本`）
7. 最后，执行系统清理，以压缩镜像空间

## 模型筛选规则

初始化向导中的模型列表，必须以 OpenRouter 的模型接口 `https://openrouter.ai/api/v1/models` 作为唯一数据源，并按以下顺序过滤：

1. 免费条件：仅保留 `pricing.prompt == 0` 且 `pricing.completion == 0` 的模型。其他价格字段（如 `web_search`、`image`、`audio`、`input_cache_read`）暂不参与“免费模型”判断。
2. 家族白名单：以模型 `id` 的前缀为准，而不是展示名称。允许的前缀与业务名映射如下：
	- `qwen/*` -> `qwen`
	- `xiaomi/*` -> `xiaomi`
	- `stepfun/*` -> `stepflash`
	- `moonshotai/*` -> `kimi`
	- `z-ai/*` -> `glm`
	- `meta-llama/*` -> `meta`
	- `google/*` -> `google`
3. 发布时间过滤：实现时优先使用 OpenRouter 返回的 `created` Unix 时间戳，作为可执行的“发布日期”标准；若 `created` 缺失，再尝试从 `canonical_slug` 中解析日期；若仍无法判断，则不进入默认候选列表，但允许用户手工输入 `model id`。
4. 时间阈值：`2025-10-01T00:00:00Z` 之后，等价于“2025 年第 3 季度之后”。
5. 默认排序：候选模型按 `created` 倒序排列，优先显示最新模型。
6. 搜索行为：GUI 搜索框只在候选集内部搜索，匹配 `id`、`name` 和归一化后的业务名前缀；同时保留手工输入 `model id` 覆盖的能力。
7. 空结果处理：若候选集为空，向导必须明确提示“当前白名单下无符合条件的免费模型”，并允许用户手工输入 `model id` 继续。

## 初始化脚本

在虚拟机定制完成，首次启动时，通过一个 python图形界面，引导用户完成 openclaw 初始化，以完成 openclaw 的个性化设置。

1. 设置openclaw 第一个 Agent 的名字(默认 Eve)，使用者的名字(默认 Quantide)
2. 设置openrouter 的 key（说明这个 key 在哪里申请，有何用处，并且加载 guest/assets/openrouter.jpg 以帮助用户理解。该图片应作为 guest payload 的一部分随 guest 目录一起复制，并由首次启动 GUI 从镜像内固定路径加载。）
3. 设置大模型 id。筛选规则见 `## 模型筛选规则`。GUI 需要展示候选列表，允许用户在候选集内按前缀、provider 别名或模型名称搜索，同时允许手工输入 `model id` 覆盖默认选择。
4. 引导用户微信扫码（或者）填写 qqbot 需要的 appId 和 appSecret。允许最终用户只用微信，或者只用 QQ，但必须至少选择其中一项。
5. 待用户添加 weixin/qq 之后，自动调用 openclaw 命令进行device pairing。在绑定之前，需要让用户看到设备信息并批准。
6. 除此之外，默认配置 duckduckgo 为搜索引擎，默认启用 tts，并使用 edge-tts 为语音引擎。
7. 调用openclaw 命令，给用户配置的 weixin/qq 发送一条配置完成，欢迎使用的消息(必须发送成功才算初始化完成)，否则提示用户加微信（quantfans_99，二维码在 guest/assets/quantfans.png，应作为 guest payload 的一部分随 guest 目录一起复制）解决。
