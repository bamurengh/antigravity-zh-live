#!/usr/bin/env bash
# Antigravity 汉化 — 一键安装 / 同步部署到 LaunchAgent
# ======================================================
# 支持两种场景：
#   · 全新 Mac（首次迁移）：自动创建 ~/.antigravity-zh 与 LaunchAgent 开机自启服务
#   · 已有环境（词典/引擎更新）：先自动备份，再同步并热升级运行中的窗口
#
# 用法: bash deploy_to_launchagent.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.antigravity-zh"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABEL="local.antigravity-zh"
PLIST="$LAUNCH_DIR/$LABEL.plist"
TS="$(date +%Y%m%d-%H%M%S)"
BK="$DST/backup-$TS"

echo "════════════════════════════════════════════════════"
echo " 部署目标: $DST"
echo " 备份目录: $BK"
echo "════════════════════════════════════════════════════"

mkdir -p "$DST" "$BK" "$LAUNCH_DIR"

# 确保本目录的 .command / .sh 具备可执行权限（防止跨机拷贝丢失权限）
chmod +x "$SRC"/*.command "$SRC"/*.sh 2>/dev/null || true

# ── 1) 备份现有部署文件 ──
for f in dict.json observer_plus.js doctor.py run_observer_plus.py; do
  [ -f "$DST/$f" ] && cp -p "$DST/$f" "$BK/$f" && echo " 备份 $f"
done
[ -f "$PLIST" ] && cp -p "$PLIST" "$BK/$LABEL.plist" && echo " 备份 plist"

# ── 2) 同步词典、引擎与注入器 ──
cp -p "$SRC/dict.json"             "$DST/dict.json"
cp -p "$SRC/observer_plus.js"      "$DST/observer_plus.js"
cp -p "$SRC/doctor.py"             "$DST/doctor.py"
cp -p "$SRC/run_observer_plus.py"  "$DST/run_observer_plus.py"

COUNT=$(python3 - "$DST/dict.json" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(len([k for k in d if not k.startswith("_")]))
PY
)
echo " 已同步: dict.json（${COUNT} 条）/ observer_plus.js / doctor.py / run_observer_plus.py"

# ── 3) 自动创建或更新 LaunchAgent plist（新机器首次迁移也能一键完成）──
python3 - "$PLIST" "$DST" "$LABEL" <<'PY'
import plistlib, sys
from pathlib import Path

plist, dst, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if plist.exists():
    data = plistlib.loads(plist.read_bytes())
else:
    data = {
        "Label": label,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(dst / "translate.log"),
        "StandardErrorPath": str(dst / "translate.log"),
    }
data["ProgramArguments"] = ["/usr/bin/python3", str(dst / "run_observer_plus.py")]
data["StandardOutPath"] = str(dst / "translate.log")
data["StandardErrorPath"] = str(dst / "translate.log")
plist.write_bytes(plistlib.dumps(data))
print(" plist 已就绪 →", " ".join(data["ProgramArguments"]))
PY

# ── 4) 启动 / 重启 LaunchAgent ──
U="$(id -u)"
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$U" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
sleep 3

echo "════════════════════════════════════════════════════"
echo " 进程状态："
pgrep -fl "antigravity-zh/" || echo "  ⚠️ 未见进程（如首次运行，请确认系统设置 → 通用 → 登录项允许后台运行）"
echo
echo " 日志尾部："
tail -12 "$DST/translate.log" 2>/dev/null || echo "  （暂无日志）"
echo
echo " 如需恢复旧版本，可直接从备份目录拷回："
echo "   $BK"
echo "════════════════════════════════════════════════════"
