#!/usr/bin/env bash
# 卸载：停 bot、关自动启动、删运行目录（漫画库默认保留）。
set -u

BASE_DIR=$(cd "$(dirname "$0")" && pwd)
WORK=${JM_WORK:-$HOME/.jm-weixin}

say() { printf '%s\n' "$*"; }

bash "$BASE_DIR/start-bot.sh" stop || true

if [ -x "$BASE_DIR/autostart/disable-autostart.sh" ]; then
  bash "$BASE_DIR/autostart/disable-autostart.sh" || true
fi

if [ -d "$WORK" ]; then
  say "[*] 删除运行目录 $WORK（日志/pid/锁）"
  rm -rf "$WORK"
fi

rm -rf "$HOME/.dsh/skills/jm-weixin-bot" 2>/dev/null || true

say "[+] 已卸载。"
say "    微信凭证还在 $HOME/.dsh/weixin/accounts/，要彻底清掉请手动删除。"
say "    漫画库（默认 $HOME/JM_Downloads）没有动。"
