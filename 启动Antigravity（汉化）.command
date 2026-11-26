#!/usr/bin/env bash
# 双击本文件 = 以「汉化模式」启动 Antigravity
# ------------------------------------------------------------
# 背景：Antigravity 2.34 起，CDP 调试端口改为「显式开启」——
#   只有带 --remote-debugging-port 启动参数时才会生成
#   ~/Library/Application Support/Antigravity/DevToolsActivePort，
#   汉化注入器正是靠这个文件找到页面的。直接点 Dock 图标启动将不会汉化。
#
# 本脚本：若 Antigravity 正在以普通方式运行 → 先让它正常退出，
#         再带上 --remote-debugging-port=0（随机端口，仅监听 127.0.0.1）重新启动。
set -u
APP="Antigravity"
PORT_FILE="$HOME/Library/Application Support/Antigravity/DevToolsActivePort"

if pgrep -x "$APP" >/dev/null 2>&1; then
  if pgrep -fl "/Applications/Antigravity.app/Contents/MacOS/Antigravity .*--remote-debugging-port" >/dev/null 2>&1; then
    echo "✓ Antigravity 已在汉化模式下运行，无需重启。"
    exit 0
  fi
  echo "▶ Antigravity 正以普通模式运行，正在退出以便重新启动…"
  osascript -e "tell application \"$APP\" to quit" >/dev/null 2>&1
  for _ in $(seq 1 30); do
    pgrep -x "$APP" >/dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -x "$APP" >/dev/null 2>&1; then
    echo "❌ Antigravity 未能退出，请手动退出（Cmd+Q）后再双击本文件。"
    exit 1
  fi
fi

echo "▶ 以汉化模式启动 Antigravity…"
open -a "$APP" --args --remote-debugging-port=0

for _ in $(seq 1 20); do
  [ -f "$PORT_FILE" ] && break
  sleep 1
done
if [ -f "$PORT_FILE" ]; then
  echo "✅ 调试端口已开启，后台注入器会在几秒内自动完成汉化。"
else
  echo "⚠️ 未检测到 DevToolsActivePort，请稍后再试或查看 ~/.antigravity-zh/translate.log"
fi
