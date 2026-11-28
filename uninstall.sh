#!/usr/bin/env bash
# Antigravity 汉化 v0.3.3 — 一键完整卸载脚本
# ==========================================
# 用法:
#   bash uninstall.sh               # 完整卸载（默认）
#   bash uninstall.sh --keep-dict   # 卸载前把 ~/.antigravity-zh/dict.json 备份到桌面
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.antigravity-zh"
LABEL="local.antigravity-zh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_FILE="$SRC/.agyzh-state"
UID_NUM="$(id -u)"
KEEP_DICT=0

for arg in "$@"; do
  case "$arg" in
    --keep-dict) KEEP_DICT=1 ;;
    -h|--help)
      echo "用法: bash uninstall.sh [--keep-dict]"
      echo "  --keep-dict  卸载前将已部署的 dict.json 备份到桌面"
      exit 0
      ;;
  esac
done

echo "════════════════════════════════════════════════════"
echo " Antigravity 汉化 — 一键卸载"
echo "════════════════════════════════════════════════════"

# 1) 可选保留自定义词典
if [ "$KEEP_DICT" -eq 1 ] && [ -f "$DST/dict.json" ]; then
  BK_DICT="$HOME/Desktop/antigravity-zh-dict-backup-$(date +%Y%m%d-%H%M%S).json"
  cp -p "$DST/dict.json" "$BK_DICT"
  echo " ✓ 已将词典备份至: $BK_DICT"
fi

# 2) 停止并注销 LaunchAgent
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || pgrep -f "antigravity-zh/run_observer_plus\.py" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  pkill -f "antigravity-zh/run_observer_plus\.py" 2>/dev/null || true
  echo " ✓ 已停止后台守护进程与 LaunchAgent"
else
  echo " · 后台守护进程未运行"
fi

# 3) 尝试卸载当前已打开页面中的注入引擎（失败不阻断）
PY_BIN="$(command -v python3 || echo /usr/bin/python3)"
if [ -x "$PY_BIN" ] && [ -f "$DST/doctor.py" ]; then
  "$PY_BIN" - "$DST" <<'PY' 2>/dev/null || true
import http.client, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from doctor import MiniWS, DEVTOOLS_PORT_FILE
if DEVTOOLS_PORT_FILE.exists():
    port = int(DEVTOOLS_PORT_FILE.read_text().strip().split("\n")[0])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", "/json/list")
    pages = [t for t in json.loads(conn.getresponse().read()) if t.get("type") == "page"]
    conn.close()
    cleaned = 0
    for p in pages:
        try:
            ws = MiniWS("127.0.0.1", port, f"/devtools/page/{p['id']}", timeout=3)
            ws.evaluate("if (typeof window.__agy_zh_uninstall === 'function') window.__agy_zh_uninstall(); delete window.__agy_zh_injected;")
            ws.close()
            cleaned += 1
        except Exception:
            pass
    if cleaned:
        print(f" ✓ 已原地卸载 {cleaned} 个运行中页面的注入引擎")
PY
fi

# 4) 删除 plist、部署目录与本地状态文件
if [ -f "$PLIST" ]; then
  rm -f "$PLIST"
  echo " ✓ 已删除 $PLIST"
fi
if [ -d "$DST" ]; then
  rm -rf "$DST"
  echo " ✓ 已删除部署目录 $DST"
fi
if [ -f "$STATE_FILE" ]; then
  rm -f "$STATE_FILE"
  echo " ✓ 已删除状态文件 $STATE_FILE"
fi

echo "════════════════════════════════════════════════════"
echo " ✅ 卸载完成！Antigravity 本体文件未被修改，无需重装。"
echo " 💡 提示：完全退出（Cmd+Q）并从 Dock 正常重启 Antigravity，"
echo "    即可关闭 CDP 调试端口并恢复 100% 原生状态。"
echo "════════════════════════════════════════════════════"
