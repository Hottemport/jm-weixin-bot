#!/usr/bin/env bash
# 开启「机器启动 → 自动拉起 bot」：把 dsh 换成包装脚本（仅适用于 DSHA/DSH 环境）。
# 非 DSH 的 Linux 请用 systemd，见 README。
set -eu

BASE_DIR=$(cd "$(dirname "$0")/.." && pwd)
TARGET=${DSH_BIN:-/usr/local/bin/dsh}
REAL_FILE=$HOME/.jm-weixin/dsh-real-bin

say() { printf '%s\n' "$*"; }

mkdir -p "$HOME/.jm-weixin"

if [ -f "$TARGET" ] && [ ! -L "$TARGET" ] && head -1 "$TARGET" 2>/dev/null | grep -q '^#!/bin/sh'; then
  say "[=] 已开启（$TARGET 已经是包装脚本）"
  exit 0
fi

REAL=$(readlink -f "$TARGET" 2>/dev/null || true)
[ -n "$REAL" ] && [ -f "$REAL" ] || { say "[x] 找不到 dsh 真实入口（$TARGET）"; exit 1; }
echo "$REAL" > "$REAL_FILE"
say "[+] 原始入口: $REAL"

rm -f "$TARGET"
cat > "$TARGET" <<EOF
#!/bin/sh
# 由 jm-weixin-bot/autostart/enable-autostart.sh 生成；disable-autostart.sh 可还原。
REAL=\$(cat "\$HOME/.jm-weixin/dsh-real-bin" 2>/dev/null)
[ -n "\$REAL" ] || REAL="$REAL"
START="$BASE_DIR/start-bot.sh"
LOG="\$HOME/.jm-weixin/autostart.log"
NODE=\$(command -v node 2>/dev/null)
[ -n "\$NODE" ] || NODE=/usr/local/bin/node
if [ -x "\$START" ]; then
  ( /bin/bash "\$START" start >>"\$LOG" 2>&1 & )
fi
exec "\$NODE" "\$REAL" "\$@"
EOF
chmod 755 "$TARGET"
say "[+] 已开启：$TARGET → 包装脚本（启动时自动起 bot）"
say "    日志: \$HOME/.jm-weixin/autostart.log"
