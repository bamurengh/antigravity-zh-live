# Antigravity 汉化工具（v0.3.3 · 适配 Antigravity 2.34+）

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
  - 内置 **1930+ 条**适配 Antigravity 2.34+ 的精选词典，支持空白归一化、大小写兜底、词典分桶快速短语匹配、动态文案匹配及 React 属性变更（`placeholder` / `title` / `aria-label` / `data-tooltip`）自动重译。
  - 严格防误翻与高性能机制：`TreeWalker` 子树剪枝 + 祖先链保护，彻底跳过代码块（含语法高亮子节点）、输入框内容、SVG、路径与 URL；短词启用防拆句保护；单批处理控制在 `12ms` 帧预算内并支持游标续扫与脱离节点自动清理，长会话不卡顿、不泄漏。
- **内嵌上下文与 Token 速度小组件**
  - 采用 Shadow DOM 样式隔离，无边框、无阴影，原生融入底部工具栏并自动适配亮色 / 暗色主题；支持 4 档窄屏自适应收缩、弹窗渲染去抖防闪烁与下拉菜单稳定锚定，缩放窗口、流式输出或切换模型时不遮挡、不跳位。
  - 实时显示上下文窗口占用（蓝 / 黄 / 红三档状态）与模型生成速率，点击可查看详细用量、单步明细，并在弹窗右上角一键原地免刷新切换中/英文（后台热升级时自动保留当前切换状态）。
- **后台守护与免重启热升级**
  - 通过 macOS `LaunchAgent` 后台自动运行，支持运行期日志自动按大小轮转，页面刷新或窗口重载时自动重新注入。
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

支持两种切换方式：

- **原地免刷新切换（推荐）**：点击输入框底部小组件展开详情卡片，点击右上角的 **`中` / `EN`** 按钮即可在当前页面即时切换中英文。
- **彻底停止/恢复后台注入**：
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

- **上下文窗口占用**：实时显示当前对话已用 Token、容量上限与占用百分比，并按负载自动变色（`<65%` 蓝、`65%~85%` 黄、`>=85%` 红）；窗口或侧边栏缩小时自动 4 档紧凑收缩，切换至非对话页面时自动隐藏。
- **Token 生成速度**：
  - **工作时**：流式输出期间按需启动 `250ms` 实时估算（`工作中 XX tok/s`）；回归空闲自动停止定时器（0 唤醒）。
  - **空闲时**：自动定格并显示上一次模型调用均速（`上次平均 XX tok/s`）。
- **交互功能**：
  - **点击指示器**：在上方弹出详情卡片，查看新增输入、缓存命中率、上次模型均速（悬停提示含工具全程均速）/ 最近单步 / 全会话总平均、近期模型调用明细（`R轮·步` 与 `工具 / 回复` 类型），支持切换窗口容量基准（`自动 / 1M / 512K / 272K / 256K / 200K`）与右上角 **`中 / EN`** 原地免刷新中英切换。
  - **独立开关**：支持 `--no-metrics`（纯汉化、不挂载小组件且不读日志）或 `--metrics-only`（英文原版 + 仅开监测小组件）。

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
python3 run_observer_plus.py                # 前台常驻运行（汉化 + 模型监测，Ctrl+C 停止）
python3 run_observer_plus.py --no-metrics   # 仅启用界面汉化，不挂载小组件且不读取本地日志
python3 run_observer_plus.py --metrics-only # 仅启用上下文与速度小组件，保持原始英文界面
python3 run_observer_plus.py --once         # 仅注入当前页面一次后退出
python3 run_observer_plus.py --log /tmp/agy_zh.log  # 将运行日志写入指定文件
```

> 部署脚本同样支持解耦模式：`bash deploy_to_launchagent.sh --no-metrics` 或 `bash deploy_to_launchagent.sh --metrics-only`。

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

运行项目根目录的一键卸载脚本即可（Antigravity 本体未被修改，无需重装）：

```bash
bash uninstall.sh               # 停止后台服务、清理页面注入并删除部署目录与 plist
bash uninstall.sh --keep-dict   # 卸载前自动把 dict.json 备份到桌面
```

卸载后完全退出（`Cmd+Q`）并从 Dock 正常重启 Antigravity，即可关闭 CDP 调试端口。
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
├── deploy_to_launchagent.sh        # 一键安装、自动备份（保留最近 3 份）与 LaunchAgent 热更新脚本
├── uninstall.sh                    # 一键完整卸载脚本（支持 --keep-dict 保留词典备份）
├── run_observer_plus.py            # 后台注入守护进程（持久 WS、单会话增量解析、休眠自愈、日志自动轮转）
├── observer_plus.js                # DOM 翻译引擎与 Shadow DOM 上下文/Token 小组件
├── dict.json                       # 汉化词典（适配 Antigravity 2.34+，1930+ 条）
├── doctor.py                       # 只读健康诊断工具（同时提供 MiniWS 给注入器复用）
├── check_project.py                # 词典质量、JS/Python/Bash 语法与增量解析一键自检工具
├── LICENSE                         # MIT 开源协议
└── README.md                       # 项目说明文档
```

---

## 安全、隐私与指标计算说明

- **零文件篡改**：不修改 `/Applications/Antigravity.app` 任何文件。
- **纯本地运行**：不访问任何外网地址，仅连接本机 `127.0.0.1` 的 CDP 调试端口，并校验 `/json/version` 归属与目标页面 URL（排除 `devtools://`、扩展页与空白页）。
- **隐私零采集**：模型监测仅增量读取当前活跃会话的 Token 数值与时间戳，**绝不读取或传输任何用户聊天正文内容**；使用 `--no-metrics` 时彻底不读取本地会话目录。
- **零第三方依赖**：仅依赖 macOS Python 3.8+ 标准库与原生浏览器 DOM API。
- **指标计算口径**：
  - **上下文窗口占用**：`input_tokens`（新增输入）+ `cache_read_tokens`（缓存命中），二者经实测互斥无重复。
  - **速度指标**：流式输出期基于 DOM 增量字符实时估算；完成后主值展示**模型生成均速**（纯模型生成耗时），鼠标悬停可查看**含工具全程均速**。

---

## 更新日志

### v0.3.3（2026-11-28）

- **注入体积与安全性修复**：修复 `%%DICT%%` 全局替换误将词典重复注入头部注释的问题（单次尾部精准替换），注入体积减少约 2/3，并彻底杜绝词典含 `*/` 导致 JS 语法中断的风险。
- **防误翻与性能升级**：为 `TreeWalker` 增加 `FILTER_REJECT` 子树剪枝与祖先链保护，彻底防止代码块语法高亮子节点被误翻；重构 `rescan()` 复用节点处理逻辑并引入游标续扫，消除长对话超预算空转；`MiniWS` 改用整块位运算掩码提速并完善 RFC 6455 Ping/二进制帧处理。
- **小组件交互优化**：详情弹窗与指示条解耦渲染，消除流式输出期每 `250ms` 重建弹窗导致的悬停提示闪烁与吞点击问题；窗口基准新增 `272K` 档位（模型上限表由 Python 单点同步注入）；热升级自动保留用户原地切换的 `EN / 中` 状态。
- **后台守护与运维加固**：`--log` 改用 `RotatingFileHandler` 实现运行期自动轮转；`parse_transcript_metrics` 预解析时间戳降低重复开销；加固 `agyzh.sh`、`deploy_to_launchagent.sh`、`uninstall.sh` 与启动脚本的进程匹配、路径空格、状态同步与残留端口清理；`check_project.py` 新增全量 Bash 语法检查、版本一致性检查与打包后 JS 语法回归校验。
- **词典完善**：补齐 2.34+ 缺失提示词条（`Applies to /btw and messages sent after this turn`、`Record Audio`、`Permissions cannot be changed while the agent is running`、`Edit Conversation Title`），清理冗余 HTML 实体键并统一术语。

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
