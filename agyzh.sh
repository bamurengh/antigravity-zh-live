#!/usr/bin/env bash
# Antigravity 汉化 — 中英切换开关
# ================================
# 用法:
#   bash agyzh.sh           # 切换（中文 ⇄ 英文）
#   bash agyzh.sh off       # 切英文
#   bash agyzh.sh on        # 切中文
#   bash agyzh.sh status    # 看当前状态
#
# 原理（为什么可靠）：
#   注入是「运行时打进页面」的，页面一刷新就全部消失。所以「切英文」不需要
#   小心翼翼地还原 DOM（那需要保存英文底本，跨越多轮翻译后必然失真），
#   只要 ①停掉自动注入 ②让页面刷新一次 —— 页面自然回到纯英文。
#   「切中文」同理：①恢复自动注入 ②刷新页面，引擎按最新词典整页重翻。
#
# 不想刷新？可以用 --soft：只停/启翻译，不要求刷新（会有残留，属已知取舍）
set -euo pipefail

LABEL="local.antigravity-zh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
AGY_DIR="$HOME/.antigravity-zh"
MODE="${1:-toggle}"
UID_NUM="$(id -u)"
# 状态文件放工作区：部署目录受系统保护，普通写会 EPERM
STATE_FILE="$(cd "$(dirname "$0")" && pwd)/.agyzh-state"   # on / off

read_state() {
  if [ -f "$STATE_FILE" ]; then cat "$STATE_FILE"; else
    # 没记录过：以进程是否存在推断
    pgrep -f "antigravity-zh/" >/dev/null 2>&1 && echo "on" || echo "off"
  fi
}

agent_running() { pgrep -f "antigravity-zh/" >/dev/null 2>&1; }

case "$MODE" in
  toggle)
    if [ "$(read_state)" = "on" ]; then MODE=off; else MODE=on; fi
    ;;
esac

case "$MODE" in
  off)
    echo "▶ 切到英文"
    if agent_running; then
      launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
      echo "  ✓ 已停止自动注入（不再往页面注入汉化）"
    else
      echo "  · 自动注入本来就是停的"
    fi
    echo "off" > "$STATE_FILE"
    echo
    echo "  请刷新一次页面（Cmd+R）→ 纯英文，无残留。"
    echo "  想直接切回中文：bash \"$0\" on"
    ;;
  on)
    echo "▶ 切到中文"
    if [ ! -f "$PLIST" ]; then echo "  ❌ 找不到 $PLIST"; exit 1; fi
    if agent_running; then
      echo "  · 自动注入本来就在运行"
      echo "on" > "$STATE_FILE"
      echo
      echo "  请刷新一次页面（Cmd+R）→ 整页按最新词典重翻为中文。"
      exit 0
    fi

    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    sleep 1
    ERR="$(mktemp)"
    OK=0
    for cmd in "launchctl bootstrap gui/$UID_NUM $PLIST" \
               "launchctl load -w $PLIST" \
               "launchctl kickstart gui/$UID_NUM/$LABEL"; do
      if $cmd 2>"$ERR"; then OK=1; break; fi
    done
    sleep 3

    if agent_running; then
      echo "on" > "$STATE_FILE"
      echo "  ✓ 已恢复自动注入（后台每 3 秒检查一次页面）"
      echo
      echo "  请刷新一次页面（Cmd+R）→ 整页按最新词典重翻为中文。"
    else
      echo "  ⚠️ 自动启动失败（launchctl 被系统拦住了）。原始错误："
      sed 's/^/     /' "$ERR" 2>/dev/null | head -3
      echo
      echo "  两个解决办法，任选一个："
      echo "   A) 给终端权限：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 勾选「终端」，"
      echo "      完全退出终端再重开，然后重新双击「恢复汉化.command」。"
      echo "   B) 先手动临时恢复（当前终端窗口里粘这条，它会常驻，别关窗口）："
      echo "      /usr/bin/python3 \"$AGY_DIR/run_observer_plus.py\" &"
      echo
      echo "  暂停状态未改变：脚本没有记录成 on，避免误报。"
      rm -f "$ERR"
      exit 1
    fi
    rm -f "$ERR"
    ;;
  status)
    WANT="$(read_state)"
    RUN="$(agent_running && echo yes || echo no)"
    echo "  期望状态 : $WANT"
    echo "  实际进程 : $([ "$RUN" = yes ] && echo 运行中 || echo 已停止)"
    if { [ "$WANT" = on ] && [ "$RUN" = no ]; } || { [ "$WANT" = off ] && [ "$RUN" = yes ]; }; then
      echo "  ⚠️ 两者不一致：可能是 launchctl 被系统拦住。重新双击对应按钮，或看 AGY 日志。"
    fi
    if [ -f "$AGY_DIR/dict.json" ]; then
      N=$(python3 -c "
import json
from pathlib import Path
d=json.loads(Path('$AGY_DIR/dict.json').read_text(encoding='utf-8'))
print(len([k for k in d if not k.startswith('_')]))" 2>/dev/null || echo "?")
      echo "  词典条数 : $N"
    fi
    ;;
  *)
    echo "用法: bash \"$0\" [toggle|on|off|status]"
    exit 1
    ;;
esac
