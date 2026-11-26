#!/usr/bin/env bash
# Antigravity 汉化 — 暂停 / 恢复（供 .command 按钮调用）
# =====================================================
# 用法: bash _agyzh_action.sh pause | resume | toggle | status
#
# 说明：本脚本是 agyzh.sh 的「按钮友好」包装：双击 .command 时会新开一个终端窗口，
# 这里负责把话说清楚（下一步该做什么），执行完停留等你看完再关窗。
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ACTION="${1:-toggle}"
MODE="$ACTION"
[ "$MODE" = "pause" ] && MODE="off"
[ "$MODE" = "resume" ] && MODE="on"

if [ ! -f "$HERE/agyzh.sh" ]; then
  echo "❌ 找不到 agyzh.sh（应与本文件在同一目录）"
  echo "   目录: $HERE"
  read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi

bash "$HERE/agyzh.sh" "$MODE"
RC=$?

echo
echo "────────────────────────────────────────"
if [ $RC -ne 0 ]; then
  echo "⚠️ 操作未成功（返回码 ${RC}），上面有具体原因和处理办法。"
elif [ "$ACTION" = "pause" ]; then
  echo "✅ 汉化已暂停。现在按 Cmd+R 刷新 Antigravity 窗口 → 界面恢复纯英文。"
  echo "   想再要中文：双击「恢复汉化.command」。"
elif [ "$ACTION" = "resume" ]; then
  echo "✅ 汉化已恢复。现在按 Cmd+R 刷新 Antigravity 窗口 → 界面重新变中文。"
  echo "   想切回英文：双击「暂停汉化.command」。"
elif [ "$ACTION" = "toggle" ]; then
  echo "✅ 已切换（详见上方）。按 Cmd+R 刷新 Antigravity 窗口生效。"
fi
echo "────────────────────────────────────────"
echo
if [ -t 0 ]; then
  read -n 1 -s -r -p "按任意键关闭此窗口…"
  echo
fi
exit $RC
