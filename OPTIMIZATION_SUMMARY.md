# 镜像大小优化总结

## 实施的优化措施

### 1. ✅ 精简 XFCE 安装包

**修改文件**: `scripts/debian-customize.sh`

**变更内容**:
- 移除: `xfce4-goodies` (约 300-500 MB)
- 保留核心组件:
  - `xfce4` - 核心桌面环境
  - `xfce4-session` - 会话管理器
  - `xfce4-panel` - 任务栏面板
  - `xfce4-settings` - 设置管理器
  - `xfwm4` - 窗口管理器
  - `thunar` - 文件管理器
  - `xfce4-terminal` - 终端模拟器
  - `xfce4-appfinder` - 应用启动器

**预期节省**: 约 300-400 MB

**功能影响**: 保留完整的桌面功能，仅移除游戏、屏保、额外主题等非必需组件

---

### 2. ✅ 优化字体安装

**修改文件**: `scripts/debian-customize.sh`

**变更内容**:
- 移除: `fonts-noto-cjk` (约 100+ MB)
- 保留: `fonts-wqy-zenhei` (约 20-30 MB)

**预期节省**: 约 70-80 MB

**功能影响**: 无影响，文泉驿正黑字体足以显示中文界面

---

### 3. ✅ 完全移除 Chrome 安装

**修改文件**: `scripts/quantideclaw-customize.sh`

**变更内容**:
- 删除 `install_chrome_best_effort()` 函数
- 删除函数调用
- arm64 已跳过，amd64 也移除安装尝试

**预期节省**:
- amd64: 约 200-300 MB
- arm64: 无变化（已跳过）

**功能影响**: 用户可按需自行安装浏览器

---

### 4. ✅ 增强清理脚本

**修改文件**:
- `scripts/debian-customize.sh`
- `scripts/quantideclaw-customize.sh`

**新增清理项**:
```bash
# 文档和手册页
rm -rf /usr/share/man/*
rm -rf /usr/share/doc/*
rm -rf /usr/share/info/*
rm -rf /usr/share/lintian/*
rm -rf /usr/share/linda/*

# 非必需的语言环境（保留中英文）
find /usr/share/locale -mindepth 1 -maxdepth 1 -type d \
    ! -name 'zh*' ! -name 'en*' ! -name 'locale.alias' \
    -exec rm -rf {} +

# npm 缓存
rm -rf /root/.npm "/home/${BUILD_USER}/.npm"

# pip 缓存
rm -rf /root/.pip "/home/${BUILD_USER}/.pip"
```

**预期节省**: 约 100-200 MB

**功能影响**: 无影响，仅删除文档、缓存和非必需语言包

---

## 预期总体效果

### 空间节省汇总

| 优化项 | 预期节省 |
|--------|---------|
| 精简 XFCE | 300-400 MB |
| 优化字体 | 70-80 MB |
| 移除 Chrome (amd64) | 200-300 MB |
| 增强清理 | 100-200 MB |
| 优化 Node.js 安装 | 50-100 MB |
| **总计** | **720-1080 MB** |

### 镜像大小预估

- **优化前**: 约 8 GB
- **优化后**: 约 **6.9-7.3 GB**
- **目标**: < 4 GB

---

### 5. ✅ 优化 Node.js 安装流程

**修改文件**: `scripts/quantideclaw-customize.sh`

**变更内容**:
- 移除 Debian 自带 Node.js 的预安装
- 直接从 NodeSource 安装 Node.js 24.x
- 避免重复安装和版本切换
- 添加成功日志信息

**预期节省**: 约 50-100 MB

**功能影响**: 无影响，直接安装所需版本，减少构建时间

---

## 下一步建议

当前优化已达到阶段一的目标，但要进一步压缩到 4 GB 以内，需要考虑：

### 选项 A：使用预装桌面的基础镜像
- 如果起始镜像已包含精简的 XFCE
- 可跳过桌面安装步骤
- 可能节省 500+ MB

### 选项 B：更激进的优化
1. **移除 Firefox** - 节省约 200-300 MB
2. **使用 LXQt 替代 XFCE** - 节省约 300-500 MB
3. **移除额外的开发工具** - 节省约 100-200 MB

### 选项 C：基础镜像优化
从更小的 Debian ISO 开始：
- 使用 `debian-mini.iso`
- 使用 netinst 镜像
- 从云镜像开始

---

## 测试清单

在合并到主分支前，需要测试：

- [ ] XFCE 最小化后是否正常启动
- [ ] 中文显示是否正常
- [ ] OpenClaw 是否正常运行
- [ ] 首次启动向导 GUI 是否正常
- [ ] 微信/QQ 插件是否正常工作
- [ ] 构建后镜像大小测量

---

## 回滚方案

如果优化导致问题，可以通过以下方式回滚：

```bash
# 恢复 XFCE 完整套件
git checkout main -- scripts/debian-customize.sh

# 恢复 Chrome 安装
git checkout main -- scripts/quantideclaw-customize.sh
```

---

## 提交信息

建议提交信息：
```
refactor: optimize image size from 8GB to ~7GB

- Replace xfce4-goodies with minimal XFCE components
- Remove fonts-noto-cjk, keep fonts-wqy-zenhei only
- Remove Chrome installation logic completely
- Enhanced cleanup: man pages, docs, unused locales

Expected size reduction: 670-980 MB
```
