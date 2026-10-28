#!/usr/bin/env bash
# 微信 JM 命令桥 · 启停脚本
#   bash start-bot.sh            启动（已在跑就跳过）
#   bash start-bot.sh status     状态 + 最近日志
#   bash start-bot.sh stop       停止
#   bash start-bot.sh restart    重启（改过脚本后用）
#
# 只在容器/机器活着时有效：机器重启后要再跑一次（自动启动见 README）。
set -u

BASE_DIR=$(cd "$(dirname "$0")" && pwd)
APP_DIR="$BASE_DIR/skill/scripts"
JM_ROOT=${JM_ROOT:-$HOME/JM_Downloads}
WORK=${JM_WORK:-$HOME/.jm-weixin}
LOG=$WORK/bot.log
PIDFILE=$WORK/bot.pid

export PYTHONIOENCODING=utf-8
# 容器里没装 tzdata（Asia/Shanghai 不生效），用 POSIX 偏移表示北京时间 UTC+8
export TZ=${JM_TZ:-UTC-8}

say() { printf '%s\n' "$*"; }

find_python() {
  command -v python3 || { say "[x] 找不到 python3"; exit 1; }
}

# 只认命令行里真的是 python 在跑 weixin_bot.py 的进程，避免把外层 bash 当成 bot
bot_pids() {
  local p cmd
  for p in $(pgrep -f 'weixin_bot.py --serve' 2>/dev/null); do
    cmd=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *python*"weixin_bot.py --serve"*) echo "$p" ;;
    esac
  done
}

running_pid() {
  local pid; pid=$(bot_pids | head -1)
  [ -n "$pid" ] || return 1
  echo "$pid"
}

ensure_config() {
  mkdir -p "$JM_ROOT"
  if [ ! -f "$JM_ROOT/option.yml" ]; then
    cat > "$JM_ROOT/option.yml" <<YAML
log: true
client:
  impl: api
  retry_times: 10
download:
  cache: true
  image:
    decode: true
    suffix: .jpg
  threading:
    image: 30
    photo: 8
dir_rule:
  base_dir: ${JM_ROOT}/
  rule: Bd / Aid
YAML
    say "[+] 已生成 $JM_ROOT/option.yml"
  fi
}

check_deps() {
  local py=$1
  "$py" - <<'PY' || exit 1
import importlib, sys
missing = []
for m in ("PIL", "Crypto", "segno", "jmcomic"):
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
if missing:
    print("[x] 缺依赖: %s" % ", ".join(missing))
    print("    bash install.sh   # 或手工: python3 -m pip install Pillow pycryptodome segno jmcomic")
    sys.exit(1)
print("[+] 依赖齐全")
PY
}

check_account() {
  local n
  n=$(ls "$HOME/.dsh/weixin/accounts/"*.json 2>/dev/null | wc -l)
  if [ "$n" -eq 0 ]; then
    say "[!] 还没有微信凭证，先扫码登录："
    say "    cd $APP_DIR && python3 send_weixin.py --login-server --port 18791"
    say "    手机/电脑浏览器打开 http://127.0.0.1:18791/"
    exit 1
  fi
  say "[+] 微信账号: $(ls "$HOME/.dsh/weixin/accounts/"*.json | sed 's#.*/##; s#\.json$##' | tr '\n' ' ')"
}

start() {
  mkdir -p "$WORK"
  # 串行化启动，避免启动钩子和手动调用同时进来开出两个实例
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$WORK/start.lock"
    flock -w 15 9 || { say "[!] 启动锁等待超时"; return 1; }
  fi
  local pid; pid=$(running_pid) && { say "[=] 已在运行 pid=$pid"; return 0; }
  local py; py=$(find_python)
  check_deps "$py"
  check_account
  ensure_config
  echo "[*] 启动 bot（root=$JM_ROOT）"
  # 后台起在当前 shell 里再 disown；9>&- 防止 bot 继承启动锁的 fd（否则锁永远不释放）
  local old_pwd=$PWD
  cd "$APP_DIR" || { say "[x] 进不去 $APP_DIR"; return 1; }
  setsid "$py" weixin_bot.py --serve --root "$JM_ROOT" >> "$LOG" 2>&1 < /dev/null 9>&- &
  disown 2>/dev/null || true
  cd "$old_pwd" 2>/dev/null || true
  sleep 2
  pid=$(running_pid)
  if [ -z "$pid" ]; then
    say "[x] 没起来，看日志: $LOG"
    tail -n 20 "$LOG"
    return 1
  fi
  echo "$pid" > "$PIDFILE"
  say "[+] 已启动 pid=$pid  日志: $LOG"
}

stop() {
  local pids pid; pids=$(bot_pids)
  if [ -z "$pids" ]; then say "[=] 没在运行"; rm -f "$PIDFILE"; return 0; fi
  for pid in $pids; do
    say "[*] 停止 pid=$pid"
    kill "$pid" 2>/dev/null
  done
  for _ in $(seq 1 20); do [ -z "$(bot_pids)" ] && break; sleep 0.3; done
  for pid in $(bot_pids); do kill -9 "$pid" 2>/dev/null; done
  rm -f "$PIDFILE"
  say "[+] 已停止"
}

status() {
  local pid; pid=$(running_pid) || { say "[-] 未运行"; [ -f "$LOG" ] && { echo "--- 最近日志"; tail -n 5 "$LOG"; }; return 0; }
  local n; n=$(bot_pids | wc -l)
  say "[+] 运行中 pid=$pid$([ "$n" -gt 1 ] && echo "（警告：共 $n 个实例，建议 restart）")"
  say "    库目录: $JM_ROOT（剩余 $(df -h "$JM_ROOT" 2>/dev/null | awk 'NR==2{print $4}')）"
  say "    日志:   $LOG"
  echo "--- 最近日志"
  tail -n 5 "$LOG"
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  *) say "用法: bash $0 [start|stop|restart|status]"; exit 2 ;;
esac
