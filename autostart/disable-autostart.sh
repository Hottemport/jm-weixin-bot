#!/usr/bin/env bash
# 关闭自动启动：把 dsh 还原成指向真实 bin.js 的软链接。
set -eu

TARGET=${DSH_BIN:-/usr/local/bin/dsh}
REAL_FILE=$HOME/.jm-weixin/dsh-real-bin

say() { printf '%s\n' "$*"; }

REAL=$(cat "$REAL_FILE" 2>/dev/null || true)
[ -n "$REAL" ] || REAL=/usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
[ -f "$REAL" ] || { say "[x] 找不到 $REAL"; exit 1; }

ln -sfn "$REAL" "$TARGET"
say "[+] 已还原：$TARGET → $REAL（自动启动已关闭）"
