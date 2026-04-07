# OpenClaw Virtual Machine Build System

基于虚拟机的 OpenClaw 分发方案，支持 macOS ARM 和 Windows x86_64。

## 方案概述

| 平台               | 虚拟化方案 | 优势                              |
| ------------------ | ---------- | --------------------------------- |
| **macOS ARM**      | UTM        | 原生 Apple Silicon 支持，性能最佳 |
| **Windows x86_64** | WSL2       | Windows 原生虚拟化，性能最佳      |

## 项目结构

```
openclaw-prebuild/
├── guest/                          # 复制到 guest 机器里的完整 payload
│   ├── scripts/
│   │   └── provision-manual.sh     # 唯一的 guest provision 入口
│   └── assets/
│       ├── openclaw_firstboot.py   # Python GUI 首次启动向导
│       ├── edge_tts_proxy.py       # 本地 TTS 代理
│       ├── openclaw-firstboot.desktop  # XFCE 自动启动项
│       ├── openrouter.jpg          # OpenRouter 指引图片
│       └── quantfans.png           # 欢迎消息失败时的兜底二维码
├── scripts/
│   ├── build-utm.sh                # macOS UTM 构建脚本
│   └── build-wsl2.bat              # Windows WSL2 构建脚本
├── assets/
│   └── debian-13.4.0-arm64-netinst.iso
└── README.md                       # 本文档
```

## 构建流程

### macOS ARM (UTM)

#### 前置要求
1. 安装 [UTM](https://mac.getutm.app) (4.0+)
2. 自己准备 Debian 13 ARM64 netinst ISO

#### 构建步骤

```bash
# 1. 克隆项目
git clone <repository-url>
cd openclaw-prebuild

# 2. 运行构建脚本
./scripts/build-utm.sh
```

构建脚本会：
1. 检查 UTM 安装
2. 引导你创建新的 UTM 虚拟机
3. 引导你完成 Debian 安装
4. 复制整个 guest/ 目录并运行 provision-manual.sh
5. 导出为 `.utm` 文件用于分发

#### 手动构建（可选）

如果你想手动操作：

1. **创建 UTM 虚拟机**
   - CPU: 4 核
   - 内存: 8192 MB
   - 磁盘: 64 GB
   - 网络: 共享网络（NAT）

2. **安装 Debian 13**
   - 使用 netinst ISO
   - 最小化安装（不装桌面）
   - 设置 root 账号: `root` / `root`
   - 普通用户可以先不创建，构建脚本会自动创建最终桌面用户 `quantide`
   - 安装 SSH 服务器

3. **运行 Provision 脚本**
   ```bash
   # 在 UTM 虚拟机内以 root 执行
   cd /path/to/openclaw-prebuild
   env TARGET_ARCH=arm64 TARGET_PLATFORM=utm BUILD_USER=quantide BUILD_USER_PASSWORD=quantide bash guest/scripts/provision-manual.sh
   ```

4. **导出虚拟机**
   - 在 UTM 中右键 VM → 导出
   - 保存为 `openclaw-debian-13.utm`

#### 手工构建（单文件 provision，推荐用于反复调试）

如果你已经能手工把 Debian 13 装起来，这条路径更稳：直接拷整个 `guest/` 目录，guest 里已经包含安装所需的脚本、图片和向导资源。

1. **在 UTM 里手工安装 Debian 13**
   - 架构选 arm64
   - 安装源用 netinst ISO
   - 软件选择阶段只保留 `standard system utilities` 和 `SSH server`
   - 不要勾选 GNOME、KDE、XFCE 等任何桌面任务
   - 安装完成后先确认能以 root 登录

2. **把整个 guest 目录拷进虚拟机**
   ```bash
   mkdir -p /root/openclaw-image
   ```
   把整个 `guest/` 目录拷到 `/root/openclaw-image/guest/`。

   目标结构应为：
   ```text
   /root/openclaw-image/
   └── guest/
       ├── scripts/
       └── assets/
   ```

3. **在 Debian guest 里执行单文件脚本**
   ```bash
   chmod +x /root/openclaw-image/guest/scripts/provision-manual.sh
   env TARGET_ARCH=arm64 TARGET_PLATFORM=utm BUILD_USER=quantide BUILD_USER_PASSWORD=quantide bash /root/openclaw-image/guest/scripts/provision-manual.sh
   ```

4. **重启并验证直接进桌面**
   ```bash
   reboot
   ```
   预期行为：启动后不落到 TTY，而是由 LightDM 自动登录到 XFCE 会话，然后立刻弹出 Python 欢迎向导。

5. **确认无误后再导出 UTM 包**
   - 先关机
   - 再在 UTM 中导出 VM

### Windows x86_64 (WSL2)

#### 前置要求
1. Windows 10/11
2. 管理员权限

#### 构建步骤

```cmd
REM 以管理员身份运行
scripts\build-wsl2.bat
```

构建脚本会：
1. 检查/安装 WSL2
2. 安装 Debian 发行版
3. 复制整个 guest/ 并运行 provision-manual.sh
4. 导出为 `.tar.gz` 文件

#### 手动构建（可选）

```powershell
# 1. 安装 WSL2 和 Debian
wsl --install
wsl --install -d Debian

# 2. 进入 Debian
wsl -d Debian

# 3. 在 WSL 内运行 provision 脚本
cd /mnt/c/path/to/openclaw-prebuild
env TARGET_ARCH=amd64 TARGET_PLATFORM=wsl2 BUILD_USER=quantide BUILD_USER_PASSWORD=quantide bash guest/scripts/provision-manual.sh

# 4. 导出 WSL 实例
wsl --export Debian openclaw-debian-13-amd64.tar
```

## Provision 脚本说明

### provision-manual.sh
**适用场景**：
- 你已经手工装好了 Debian，只想把 OpenClaw 桌面环境一次性打进去
- 你不想再维护多份 shell 脚本
- 你想把构建问题收敛到“复制一个 guest 目录”

**功能**：
- 配置 APT、npm、pip、bun 中国大陆镜像
- 优先使用 Debian 镜像安装满足 OpenClaw 要求的 Node.js；只有版本不足时才回退到 NodeSource 24.x
- 安装 XFCE 桌面、Firefox ESR
- amd64 上 best-effort 安装 Google Chrome
- UTM 上配置 LightDM 自动登录直进桌面
- WSL2 上安装 shell 会话触发的首次启动入口，不依赖 LightDM
- 安装 OpenClaw CLI 和微信插件
- 安装首次启动 Python 向导、Edge TTS 代理和 XFCE 自动启动项
- 清理缓存并为镜像导出做收尾

**环境变量**：
- `TARGET_ARCH`: 目标架构 (amd64/arm64)
- `TARGET_PLATFORM`: `utm` 或 `wsl2`
- `BUILD_USER`: 系统用户名 (默认: quantide)
- `BUILD_USER_PASSWORD`: 系统用户密码 (默认: quantide)
- `INSTALL_DESKTOP`: 是否安装桌面 (默认: true)
- `RUN_CLEANUP`: 是否在结尾执行清理 (默认: true)

### 为什么把图片放进 guest/assets 更合理
- 这些图片是 guest 运行时资源，不是宿主机构建资源。
- `openclaw_firstboot.py` 在 guest 里直接从固定路径读取它们，所以它们应该和向导脚本放在一起。
- 这样构建与热修复都可以统一成“复制整个 guest/”，不必再额外维护一份根目录 `assets/` 里的图片清单。
- 根目录 `assets/` 现在只保留宿主机侧需要的大文件，比如 Debian ISO。

## 首次启动体验

当客户启动 VM 时：
1. 自动登录到 XFCE 桌面
2. Python GUI 向导自动启动
3. 引导完成 OpenClaw 配置：
   - Agent 名称和用户名
   - OpenRouter API Key
   - 大模型选择
   - 可选的微信/QQBot 插件
   - 设备配对审批
4. 配置完成后，向导不再自动启动

### 原理说明：为什么会“一开机就进桌面，再自动弹欢迎向导”

这套机制分成两层：

1. **进桌面**
   - `provision-manual.sh` 会安装 XFCE；在 UTM 上还会安装并配置 LightDM。
   - 脚本写入 `/etc/lightdm/lightdm.conf.d/50-openclaw-autologin.conf`，指定 `autologin-user=quantide` 和 `user-session=xfce`。
   - 脚本再执行 `systemctl set-default graphical.target`，所以机器启动目标不再是纯字符登录，而是图形会话。
   - LightDM 启动后会自动登录指定用户，直接落到 XFCE 桌面。

2. **弹欢迎向导**
   - 脚本把 [guest/assets/openclaw-firstboot.desktop](guest/assets/openclaw-firstboot.desktop) 安装到 `/etc/xdg/autostart/` 和用户自己的 `~/.config/autostart/`。
   - XFCE 每次建立桌面会话时都会扫描这些 `.desktop` 文件，并执行其中的 `Exec=/usr/local/bin/openclaw-firstboot-launch`。
   - `openclaw-firstboot-launch` 只负责防重入检查，再异步拉起 `/usr/local/bin/openclaw-firstboot`。
   - `openclaw-firstboot` 会先检查 `/var/lib/openclaw-firstboot/completed`。如果没有完成标记，就运行 [guest/assets/openclaw_firstboot.py](guest/assets/openclaw_firstboot.py)；如果已经完成，就直接退出。
   - Python 向导只有在“欢迎消息发送成功”后才会写入完成标记，所以未完成前它会继续在每次首次桌面会话时出现；完成后则不再弹出。

## 分发给客户

### macOS
```bash
# 压缩 UTM 文件
tar -czf openclaw-debian-13-arm64.utm.tar.gz openclaw-debian-13-arm64.utm

# 客户安装
# 1. 解压 tar.gz
# 2. 双击 .utm 文件导入到 UTM
# 3. 启动 VM
```

### Windows
```powershell
# 客户导入 WSL 实例
wsl --import OpenClaw .\openclaw-wsl\ .\openclaw-debian-13-amd64.tar

# 启动
wsl -d OpenClaw
```

## 故障排除

### UTM 构建失败
- 确保 UTM 4.0+ 已安装
- 检查 ISO 文件完整性
- 查看 UTM 日志获取详细错误

### WSL2 构建失败
- 确保以管理员身份运行
- 检查 Windows 版本支持 WSL2
- 运行 `wsl --update` 更新 WSL

### Provision 脚本失败
- 检查网络连接（需要下载软件包）
- 查看 `/tmp/` 下的脚本输出日志
- 确保有足够的磁盘空间

## 定制和扩展

### 修改默认配置
编辑 `guest/scripts/provision-manual.sh` 中生成 installer env 的部分，或直接修改 `guest/assets/openclaw_firstboot.py`。

### 添加额外软件
在对应的 provision 脚本中添加安装逻辑。

### 修改首次启动向导
编辑 `guest/assets/openclaw_firstboot.py`。

## 许可

本项目用于 OpenClaw 虚拟化部署。
