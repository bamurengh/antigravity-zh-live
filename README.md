# Antigravity 汉化工具（适配 Antigravity 2.34+）

轻量、安全、非侵入式的 Antigravity 界面实时汉化方案。不修改 Antigravity 任何安装文件，纯本地运行，仅通过本机 `127.0.0.1` CDP 调试接口注入 DOM 翻译脚本。

同时内置**上下文窗口与 Token 速度小组件**：在对话输入框底部原生嵌入轻量指示器，实时显示上下文占用百分比与 Token 生成速度（`tok/s`），点击可展开详情卡片查看新增输入、缓存命中率与近期模型调用明细，并支持切换窗口容量基准。

---

## 快速安装（macOS）

> **环境要求**：macOS、Antigravity 2.34+、系统自带 `/usr/bin/python3`（纯标准库实现，**无需安装任何 `pip` 或 `npm` 依赖**）。

### 1. 获取项目代码

使用 Git 克隆仓库（或从 [Releases](https://github.com/bamurengh/antigravity-zh-live/releases) 下载压缩包并解压）：

```bash
git clone https://github.com/bamurengh/antigravity-zh-live.git
cd antigravity-zh-live
```

### 2. 运行一键部署脚本

```bash
bash deploy_to_launchagent.sh
```

部署脚本会自动完成以下操作：
1. 将词典、翻译引擎与守护进程同步到 `~/.antigravity-zh`（如已有旧版，会先自动备份到 `~/.antigravity-zh/backup-YYYYMMDD-HHMMSS`）。
2. 自动生成并注册 macOS 后台服务 `~/Library/LaunchAgents/local.antigravity-zh.plist`。
3. 立即启动（或热重启）后台守护进程并输出运行状态与日志尾部。

> **首次安装提示**：如果 macOS 弹窗提示后台项目权限，请前往 **系统设置 → 通用 → 登录项**，确保允许该后台项目运行。

---

## 核心特性

- **非侵入式实时汉化**
  - 不修改 `/Applications/Antigravity.app` 任何二进制或资源文件，升级 Antigravity 无需重新打补丁。
  - 内置 **1935 条**适配 Antigravity 2.34+ 的精选词典，支持空白归一化、大小写兜底、动态文案匹配及 React 属性变更（`placeholder` / `title` / `aria-label` / `data-tooltip`）自动重译。
  - 严格防误翻机制：自动跳过代码块、输入框、路径与 URL，对短词启用防拆句保护，且单批处理控制在 `12ms` 帧预算内，界面零卡顿。
- **内嵌上下文与 Token 速度小组件**
  - 采用 Shadow DOM 样式隔离，无边框、无阴影，原生融入底部工具栏并自动适配亮色 / 暗色主题。
  - 实时显示上下文窗口占用（绿 / 黄 / 红三档状态）与模型生成速率，点击可查看详细用量与单步明细。
- **后台守护与免重启热升级**
  - 通过 macOS `LaunchAgent` 后台自动运行，页面刷新或窗口重载时自动重新注入。
  - 修改 `dict.json` 或 `observer_plus.js` 后，执行部署脚本即可自动备份旧版本并在运行中的窗口完成热替换，**无需重启 Antigravity**。
- **一键中英切换**
  - 提供可直接双击的 `.command` 快捷入口与命令行工具 `agyzh.sh`，随时在中文与纯英文界面之间无残留切换。
- **内置链路体检工具**
  - 提供只读诊断脚本 `doctor.py`，逐层排查运行环境、调试端口、CDP 连接、注入状态与实时汉化覆盖率。

---

## 日常使用指南

### 1. 启动 Antigravity（关键）

从 Antigravity 2.34 起，CDP 调试端口需要显式开启。请**双击项目目录下的**：

- **`启动Antigravity（汉化）.command`**

> - 如果之前直接从 Dock 打开了普通模式，双击该脚本会自动正常退出 Antigravity，并以 `--remote-debugging-port=0`（随机端口，仅监听 `127.0.0.1`）重新启动。
> - 后台守护进程会在几秒内检测到调试端口并自动完成汉化注入。

### 2. 暂停 / 恢复汉化（中英一键切换）

因为翻译脚本是运行时注入页面的，切换回纯英文无需复杂还原 DOM，做到零残留：

- **切回纯英文**：双击 **`暂停汉化.command`**，然后在 Antigravity 窗口按 `Cmd+R` 刷新即可。
- **恢复中文**：双击 **`恢复汉化.command`**，然后在 Antigravity 窗口按 `Cmd+R` 刷新即可。

也可以使用命令行等效操作：

```bash
bash agyzh.sh status   # 查看后台服务状态、期望开关与词典条数
bash agyzh.sh off      # 暂停汉化（切回英文）
bash agyzh.sh on       # 恢复汉化（切回中文）
bash agyzh.sh toggle   # 中英状态切换
```

### 3. 内嵌上下文窗口与 Token 速度小组件

在汉化激活状态下，对话输入框底部「选择模型」按钮右侧会自动嵌入一个原生风格的轻量指示器：

- **上下文窗口占用**：实时显示当前对话已用 Token、容量上限与占用百分比，并按负载自动变色（`<50%` 绿、`50%~75%` 黄、`>=75%` 红）；切换至非对话页面时自动隐藏。
- **Token 生成速度**：
  - **工作时**：流式输出期间每 `250ms` 实时跳动估算（`实时 ~XX tok/s`），多步推理间隙显示当前轮步速（`工作中 XX tok/s`），静默思考阶段显示 `思考生成中…`。
  - **空闲时**：自动定格并注明上一次提问完整过程的平均生成速度（`上次均速 XX tok/s`）。
- **交互功能**：
  - **点击指示器**：在上方弹出详情卡片，查看新增输入、缓存命中率、上次均速 / 最近单步 / 全会话总均速、近期模型调用明细（`第X轮·步` 与 `工具 / 回复` 类型），并可手动切换窗口容量基准（`自动 / 1M / 512K / 256K / 200K`）。
  - **临时隐藏 / 关闭**：在开发者工具控制台执行 `window.__agy_hud_enabled = false` 可临时隐藏；或在手动启动注入器时附加 `--no-metrics` 关闭指标推送。

---

## 自定义词典与高级用法

### 修改词典或引擎（免重启热更新）

直接编辑项目目录中的 `dict.json`（按 `"英文原文": "中文译文"` 格式增改）或 `observer_plus.js`，然后执行一次部署脚本即可自动同步并热升级运行中的窗口（**无需重启 Antigravity**）：

```bash
# 在项目根目录下执行
bash deploy_to_launchagent.sh
```

### 手动运行守护进程（调试 / 临时使用）

如果不使用 LaunchAgent，也可以直接在前台手动运行注入器：

```bash
python3 run_observer_plus.py               # 前台常驻运行（Ctrl+C 停止）
python3 run_observer_plus.py --once        # 仅注入当前页面一次后退出
python3 run_observer_plus.py --no-metrics  # 仅启用界面汉化，关闭小组件指标推送
python3 run_observer_plus.py --log /tmp/agy_zh.log  # 将运行日志写入指定文件
```

---

## 诊断与常见问题

### 健康体检工具

若遇到未汉化、连接异常或想查看当前界面汉化覆盖率，可运行只读诊断工具：

```bash
python3 doctor.py          # 完整 8 项链路体检（含环境、端口、CDP、注入状态与覆盖率）
python3 doctor.py --json   # 以 JSON 格式输出诊断结果
```

### 常见问题

<details>
<summary><b>1. 从 Dock 直接打开 Antigravity 后没有汉化？</b></summary>

Antigravity 2.34 起默认不开启 CDP 调试端口。请改用双击 **`启动Antigravity（汉化）.command`** 启动（若已在普通模式运行，双击它会自动重启并带参开启端口）。
</details>

<details>
<summary><b>2. 双击「恢复汉化.command」提示 launchctl 被系统拦截？</b></summary>

这是 macOS 后台权限或 TCC 限制所致，可按以下任一方式处理：
1. 前往 **系统设置 → 通用 → 登录项**，允许后台服务运行。
2. 前往 **系统设置 → 隐私与安全性 → 完全磁盘访问权限**，勾选「终端」后完全退出终端重开，再重新执行脚本。
</details>

<details>
<summary><b>3. 暂停汉化后页面上还有部分中文？</b></summary>

停止后台注入后，页面内存中已注入的脚本需随页面重载清除。在 Antigravity 窗口按 **`Cmd+R`** 刷新一次，即可恢复 100% 纯英文界面。
</details>

<details>
<summary><b>4. 如何彻底卸载？</b></summary>

依次卸载 LaunchAgent 服务并删除本地部署目录即可（Antigravity 本体未被修改，无需重装）：

```bash
launchctl bootout "gui/$(id -u)/local.antigravity-zh" 2>/dev/null || true
rm -f ~/Library/LaunchAgents/local.antigravity-zh.plist
rm -rf ~/.antigravity-zh
```
</details>

---

## 目录结构

```text
antigravity-zh-live/
├── 启动Antigravity（汉化）.command  # 日常入口：以开启 CDP 端口的汉化模式启动 Antigravity
├── 暂停汉化.command                 # 快捷脚本：一键暂停汉化（切回英文）
├── 恢复汉化.command                 # 快捷脚本：一键恢复汉化（切回中文）
├── agyzh.sh                        # 命令行开关：toggle / on / off / status
├── _agyzh_action.sh                # 供 .command 双击调用的交互包装脚本
├── deploy_to_launchagent.sh        # 一键安装、自动备份与 LaunchAgent 同步热更新脚本
├── run_observer_plus.py            # 后台注入守护进程（刷新重注、版本热升级、指标推送）
├── observer_plus.js                # DOM 翻译引擎与 Shadow DOM 上下文/Token 小组件
├── dict.json                       # 汉化词典（适配 Antigravity 2.34+，1935 条）
├── doctor.py                       # 只读健康诊断工具（同时提供 MiniWS 给注入器复用）
├── LICENSE                         # MIT 开源协议
└── README.md                       # 项目说明文档
```

---

## 安全与隐私说明

- **零文件篡改**：不修改 `/Applications/Antigravity.app` 任何文件。
- **纯本地运行**：不访问任何外网地址，仅连接本机 `127.0.0.1` 的 CDP 调试端口。
- **零第三方依赖**：仅依赖 macOS 系统自带 Python 3 标准库与原生浏览器 DOM API。
- **内容保护**：自动跳过 `script` / `style` / `code` / `pre` / `input` / `textarea` / `select` / `option` / `svg` / `kbd` / `samp`、代码块、本地路径和 URL，绝不污染对话正文与代码内容。

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
