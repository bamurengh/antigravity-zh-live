#!/usr/bin/env bash
# Antigravity 汉化 v0.3.3 — 一键安装 / 同步部署到 LaunchAgent
# ==============================================================
# 支持两种场景：
#   · 全新 Mac（首次迁移）：自动创建 ~/.antigravity-zh 与 LaunchAgent 开机自启服务
#   · 已有环境（词典/引擎更新）：先自动备份（最多保留 3 份），再同步并热升级运行中的窗口
#
# 用法:
#   bash deploy_to_launchagent.sh                 # 默认：汉化 + 模型监测
#   bash deploy_to_launchagent.sh --no-metrics    # 仅汉化（不挂载小组件，不读日志）
#   bash deploy_to_launchagent.sh --metrics-only  # 仅模型监测（保持英文原版界面）
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.antigravity-zh"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
LABEL="local.antigravity-zh"
PLIST="$LAUNCH_DIR/$LABEL.plist"
TS="$(date +%Y%m%d-%H%M%S)"
BK="$DST/backup-$TS"
EXTRA_FLAG=""

for arg in "$@"; do
  case "$arg" in
    --no-metrics|--metrics-only) EXTRA_FLAG="$arg" ;;
    -h|--help)
      echo "用法: bash deploy_to_launchagent.sh [--no-metrics | --metrics-only]"
      exit 0
      ;;
  esac
done

# ── 0) 统一探测可用的 Python 3.8+ 解释器 ──
PY_BIN=""
for cand in "/usr/bin/python3" "$(command -v python3 2>/dev/null || true)" "/opt/homebrew/bin/python3" "/usr/local/bin/python3"; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY_BIN="$cand"
    break
  fi
done
if [ -z "$PY_BIN" ]; then
  echo "❌ 未找到可用的 Python 3.8+ 解释器，请先安装 Python 3 或 Xcode Command Line Tools。"
  exit 1
fi

echo "════════════════════════════════════════════════════"
echo " 部署目标: $DST"
echo " Python  : $PY_BIN"
echo " 备份目录: $BK"
[ -n "$EXTRA_FLAG" ] && echo " 运行模式: $EXTRA_FLAG"
echo "════════════════════════════════════════════════════"

mkdir -p "$DST" "$BK" "$LAUNCH_DIR"

# 确保本目录的 .command / .sh 具备可执行权限（防止跨机拷贝丢失权限）
chmod +x "$SRC"/*.command "$SRC"/*.sh 2>/dev/null || true

# ── 1) 备份现有部署文件，并自动清理旧备份（仅保留最近 3 份）──
for f in dict.json observer_plus.js doctor.py run_observer_plus.py; do
  [ -f "$DST/$f" ] && cp -p "$DST/$f" "$BK/$f" && echo " 备份 $f"
done
[ -f "$PLIST" ] && cp -p "$PLIST" "$BK/$LABEL.plist" && echo " 备份 plist"
"$PY_BIN" - "$DST" <<'PY'
import shutil, sys
from pathlib import Path
dst = Path(sys.argv[1])
bks = sorted([p for p in dst.glob("backup-*") if p.is_dir()], key=lambda p: p.name, reverse=True)
for old in bks[3:]:
    shutil.rmtree(old, ignore_errors=True)
PY

# ── 2) 同步词典、引擎与注入器 ──
cp -p "$SRC/dict.json"             "$DST/dict.json"
cp -p "$SRC/observer_plus.js"      "$DST/observer_plus.js"
cp -p "$SRC/doctor.py"             "$DST/doctor.py"
cp -p "$SRC/run_observer_plus.py"  "$DST/run_observer_plus.py"

COUNT=$("$PY_BIN" - "$DST/dict.json" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(len([k for k in d if not k.startswith("_")]))
PY
)
echo " 已同步: dict.json（${COUNT} 条）/ observer_plus.js / doctor.py / run_observer_plus.py"

# ── 3) 自动创建或更新 LaunchAgent plist（新机器首次迁移也能一键完成）──
"$PY_BIN" - "$PLIST" "$DST" "$LABEL" "$PY_BIN" "$EXTRA_FLAG" <<'PY'
import plistlib, sys
from pathlib import Path

plist, dst, label, py_bin, extra_flag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
if plist.exists():
    data = plistlib.loads(plist.read_bytes())
else:
    data = {
        "Label": label,
        "RunAtLoad": True,
        "KeepAlive": True,
    }
prog = [py_bin, str(dst / "run_observer_plus.py"), "--log", str(dst / "translate.log")]
if extra_flag:
    prog.append(extra_flag)
data["ProgramArguments"] = prog
# 正常日志由 Python RotatingFileHandler 自动按大小轮转写入 translate.log；
# StandardErrorPath 仅用于兜底捕获 Python 未捕获异常或启动期错误。
data["StandardOutPath"] = "/dev/null"
data["StandardErrorPath"] = str(dst / "translate.err.log")
plist.write_bytes(plistlib.dumps(data))
print(" plist 已就绪 →", " ".join(data["ProgramArguments"]))
PY

# ── 4) 启动 / 重启 LaunchAgent ──
U="$(id -u)"
PROC_PATTERN="antigravity-zh/run_observer_plus\.py"
LAUNCH_FAILED=0
launchctl bootout "gui/$U/$LABEL" 2>/dev/null || true
sleep 1
ERR_FILE="$(mktemp)"
if ! launchctl bootstrap "gui/$U" "$PLIST" 2>"$ERR_FILE"; then
  if ! launchctl load -w "$PLIST" 2>>"$ERR_FILE"; then
    LAUNCH_FAILED=1
    echo " ⚠️ launchctl 启动返回错误："
    sed 's/^/    /' "$ERR_FILE" | head -n 5
  fi
fi
rm -f "$ERR_FILE"
sleep 3

echo "════════════════════════════════════════════════════"
echo " 进程状态："
if pgrep -fl "$PROC_PATTERN"; then
  echo "on" > "$SRC/.agyzh-state" 2>/dev/null || true
else
  LAUNCH_FAILED=1
  echo "  ⚠️ 未见进程（如首次运行，请确认系统设置 → 通用 → 登录项允许后台运行）"
fi
echo
echo " 日志尾部："
tail -12 "$DST/translate.log" 2>/dev/null || echo "  （暂无日志）"
if [ -s "$DST/translate.err.log" ]; then
  echo
  echo " 错误日志尾部（translate.err.log）："
  tail -6 "$DST/translate.err.log" 2>/dev/null || true
fi
echo
echo " 如需恢复旧版本，可直接从备份目录拷回："
echo "   $BK"
echo "════════════════════════════════════════════════════"
exit "$LAUNCH_FAILED"
