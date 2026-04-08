目标是把一个最基础的 Debian 13 虚拟机，定制成可发布的 `QuantideClaw` 镜像，并支持后续高频发布产品版本。

重构后的方案采用“两阶段构建”：

1. 第一阶段构建“基础品牌系统镜像”，低频更新，重点是操作系统、桌面环境、品牌启动体验。
2. 第二阶段构建“QuantideClaw 产品镜像”，高频更新，重点是 OpenClaw、插件和首次启动引导。

## 总体原则

- 基础系统使用 Debian 13。
- UTM 场景优先支持 `arm64`。
- 用户提供的初始镜像只要求具备：
  - SSH
  - 基础系统工具
  - 可用的 root 访问
  - 尚未安装 XFCE
- 最终交付镜像启动时应：
  - 显示 `QuantideClaw` 品牌启动图
  - 自动进入 XFCE 桌面
  - 首次登录后自动执行 `onboard.py`
  - `onboard.py` 完成后写入完成标记，后续重启不再重复执行

## 两阶段脚本

### 第一阶段：`debian-customize.sh`

职责是把“最基础 Debian”改造成“基础品牌系统镜像”。

必须完成：

1. 切换 APT 等系统级软件源到中国大陆镜像。
2. 安装 XFCE 桌面、LightDM、Firefox ESR 以及基础图形运行依赖。
3. 创建并配置最终桌面用户，支持自动登录。
4. 隐藏 GRUB 启动菜单，抑制启动调试输出。
5. 安装并启用 Plymouth 品牌启动图，统一使用 `QuantideClaw` 品牌资源。
6. 设置系统默认进入图形桌面。
7. 在脚本结束时执行安全清理，尽量缩小基础镜像体积。

第一阶段不负责：

- 安装 OpenClaw
- 安装 OpenClaw 插件
- 部署首次启动向导业务逻辑

第一阶段产物：

- 一个低频更新的基础镜像
- 已具备品牌启动体验
- 已能自动进入 XFCE 桌面
- 可作为后续产品构建的稳定底座

### 第二阶段：`quantideclaw-customize.sh`

职责是把“基础品牌系统镜像”改造成“可发布的 QuantideClaw 产品镜像”。

必须完成：

1. 配置 npm、pip、bun 等开发/运行时镜像源到中国大陆镜像。
2. 安装 OpenClaw CLI 及其运行依赖。
3. 安装微信/QQ 等所需插件。
4. 部署 `onboard.py` 以及它依赖的图片、桌面自启动入口和包装脚本。
5. 配置首次登录自动运行 `onboard.py`。
6. `onboard.py` 成功完成后写入 marker，之后重启或再次登录均跳过。
7. 在脚本结束时执行安全清理，尽量缩小可发布镜像体积。

第二阶段不负责：

- 再次安装桌面环境
- 再次配置 GRUB/Plymouth
- 改变基础镜像的系统级品牌策略

第二阶段产物：

- 一个可 clone、可导出、可对外分发的 QuantideClaw 产品镜像

## 交付流程

推荐流程如下：

1. 用户准备一个最基础的 Debian 13 虚拟机。
2. 运行 `debian-customize.sh`，得到基础品牌系统镜像。
3. 对基础镜像做快照或保留一份长期基线。
4. 运行 `quantideclaw-customize.sh`，得到产品镜像。
5. 将产品镜像 clone 出来发布给最终用户。
6. 最终用户首次在 UTM 中启动时，自动进入桌面并运行 `onboard.py`。

## 阶段执行方式

为了避免构建时混淆，两个阶段都按“在宿主机调用脚本，由脚本自动上传资源并通过 SSH 在虚拟机内执行”的方式运行。

### 第一阶段执行方式

1. 在宿主机仓库根目录运行 `bash ./scripts/debian-customize.sh`。
2. 脚本提示输入虚拟机 IP、SSH 用户、SSH 端口等登录信息。
3. 脚本自动上传第一阶段所需的脚本和品牌资源到虚拟机临时目录。
4. 脚本通过 SSH 在虚拟机内以当前登录用户执行 `debian-customize.sh`；推荐直接使用 `root` 登录。
5. 运行结束后重启，验证品牌启动图、自动登录和桌面可用性。
6. 验证通过后，将该虚拟机保留为基础品牌系统镜像。

### 第二阶段执行方式

1. 从第一阶段产物 clone 一个工作副本。
2. 在宿主机仓库根目录运行 `bash ./scripts/quantideclaw-customize.sh`。
3. 脚本提示输入虚拟机 IP、SSH 用户、SSH 端口等登录信息。
4. 脚本自动上传第二阶段所需的脚本和产品资源到虚拟机临时目录。
5. 脚本通过 SSH 在虚拟机内以当前登录用户执行 `quantideclaw-customize.sh`；推荐直接使用 `root` 登录。
6. 运行结束后重启，验证首次登录是否自动拉起 `onboard.py`。
7. 验证通过后，将该工作副本作为最终发布镜像导出或 clone。

## 清理约束

两个阶段都必须在结束时执行安全清理，但必须遵守以下限制。

允许的清理：

- `apt-get autoremove --purge`
- `apt-get clean`
- 删除 `/var/lib/apt/lists/*`
- 删除 `/tmp/*` 和 `/var/tmp/*`
- 删除 root 和目标桌面用户的缓存目录
- 清理常见包管理器缓存
- 删除 `__pycache__`
- 裁剪日志文件

禁止的清理：

- 不得进行任何形式的磁盘 0 填充
- 不得使用 `dd if=/dev/zero`、`fallocate`、临时大文件占满磁盘等危险做法
- 不得为了压缩镜像而消耗宿主机剩余磁盘空间
- 默认不执行 `fstrim`

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

## 首次启动脚本

在第二阶段完成后，首次启动时通过一个 Python 图形界面 `onboard.py` 引导用户完成 QuantideClaw 初始化。

1. 设置openclaw 第一个 Agent 的名字(默认 Eve)，使用者的名字(默认 Quantide)
2. 设置openrouter 的 key（说明这个 key 在哪里申请，有何用处，并且加载 guest/assets/openrouter.jpg 以帮助用户理解。该图片应作为 guest payload 的一部分随 guest 目录一起复制，并由首次启动 GUI 从镜像内固定路径加载。）
3. 设置大模型 id。筛选规则见 `## 模型筛选规则`。GUI 需要展示候选列表，允许用户在候选集内按前缀、provider 别名或模型名称搜索，同时允许手工输入 `model id` 覆盖默认选择。
4. 引导用户微信扫码（或者）填写 qqbot 需要的 appId 和 appSecret。允许最终用户只用微信，或者只用 QQ，但必须至少选择其中一项。
5. 待用户添加 weixin/qq 之后，自动调用 openclaw 命令进行device pairing。在绑定之前，需要让用户看到设备信息并批准。
6. 除此之外，默认配置 duckduckgo 为搜索引擎，默认启用 tts，并使用 edge-tts 为语音引擎。
7. 调用openclaw 命令，给用户配置的 weixin/qq 发送一条配置完成，欢迎使用的消息(必须发送成功才算初始化完成)，否则提示用户加微信（quantfans_99，二维码在 guest/assets/quantfans.png，应作为 guest payload 的一部分随 guest 目录一起复制）解决。
