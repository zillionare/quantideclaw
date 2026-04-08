# QuantideClaw VM Build System

本项目用于把一个最基础的 Debian 13 虚拟机，制作成可交付的 `QuantideClaw` 镜像。

当前采用“两阶段构建”：

1. 第一阶段生成一个低频更新的“基础品牌系统镜像”。
2. 第二阶段在基础镜像上安装 QuantideClaw 产品，并生成可发布镜像。

`README.md` 只保留开发者操作步骤；职责边界、技术约束和首次启动规则统一维护在 `spec.md`。

## 操作前准备

开始前请确认：

1. 已在 UTM 中安装好一台最基础的 Debian 13。
2. 虚拟机已经开机，并且可以通过 SSH 登录。
3. 本仓库位于宿主机，本地可执行 `bash ./scripts/debian-customize.sh` 与 `bash ./scripts/quantideclaw-customize.sh`。
4. 如需自定义桌面用户名和密码，提前准备好 `BUILD_USER` 与 `BUILD_USER_PASSWORD`。

## 第一阶段：制作基础品牌系统镜像

使用脚本：`scripts/debian-customize.sh`

1. 启动 Debian 虚拟机，确认 SSH 服务可用，并记下虚拟机 IP。
2. 在宿主机仓库根目录执行：

```bash
bash ./scripts/debian-customize.sh
```

3. 按提示输入：

- 虚拟机 IP 或主机名
- SSH 用户，建议 `root`
- SSH 端口，默认 `22`
- SSH 密码会由系统 `ssh` 在终端中提示输入

4. 如需自定义桌面用户名和密码，可在宿主机这样运行：

```bash
BUILD_USER=quantide BUILD_USER_PASSWORD=quantide \
  bash ./scripts/debian-customize.sh
```

脚本会自动把第一阶段需要的脚本和品牌资源上传到虚拟机并远端执行，你不需要手工复制任何文件。
如宿主机已安装 `sshpass`，也可以这样避免重复输入 SSH 密码：

```bash
SSH_PASSWORD=root \
  BUILD_USER=quantide BUILD_USER_PASSWORD=quantide \
  bash ./scripts/debian-customize.sh
```

5. 脚本完成后重启：

```bash
reboot
```

6. 重启后逐项验收：

- 启动时显示 `QuantideClaw` 品牌画面
- 不再停留在 GRUB 菜单
- 自动进入 XFCE 桌面
- 桌面用户可以正常登录并使用

7. 验收通过后关机，并将该虚拟机保留为“基础品牌系统镜像”。

## 第二阶段：制作可发布产品镜像

使用脚本：`scripts/quantideclaw-customize.sh`

1. 从第一阶段完成后的基础镜像 clone 一个新的工作副本。
2. 启动该副本，确认 SSH 服务可用，并记下虚拟机 IP。
3. 在宿主机仓库根目录执行：

```bash
bash ./scripts/quantideclaw-customize.sh
```

4. 按提示输入：

- 虚拟机 IP 或主机名
- SSH 用户，建议 `root`
- SSH 端口，默认 `22`
- SSH 密码会由系统 `ssh` 在终端中提示输入

5. 如需自定义桌面用户名和密码，可在宿主机这样运行：

```bash
BUILD_USER=quantide BUILD_USER_PASSWORD=quantide \
  bash ./scripts/quantideclaw-customize.sh
```

脚本会自动把第二阶段需要的脚本和产品资源上传到虚拟机并远端执行，你不需要手工复制任何文件。
如宿主机已安装 `sshpass`，也可以这样避免重复输入 SSH 密码：

```bash
SSH_PASSWORD=root \
  BUILD_USER=quantide BUILD_USER_PASSWORD=quantide \
  bash ./scripts/quantideclaw-customize.sh
```

6. 脚本完成后重启：

```bash
reboot
```

7. 重启后逐项验收：

- 仍然显示 `QuantideClaw` 品牌画面
- 自动进入 XFCE 桌面
- 首次登录自动运行 `onboard.py`
- 完成初始化后再次重启，不再重复弹出 `onboard.py`
- OpenClaw 与所需插件可正常工作

8. 验收通过后关机，导出或 clone 该虚拟机作为最终交付镜像。

## 推荐发布流程

1. 长期维护一份第一阶段完成后的基础镜像。
2. 每次发版都从基础镜像 clone 一个新副本。
3. 在新副本中执行第二阶段脚本。
4. 完成首次启动验收后导出 `.utm` 或直接分发副本。

## 补充说明

- 两个阶段结束时都会执行安全清理，但不会进行磁盘 0 填充。
- `build-utm.sh` 与 `build-wsl2.bat` 仅保留为旧流程参考，不再作为主线构建入口。
- 技术细节请查看 `spec.md`。
