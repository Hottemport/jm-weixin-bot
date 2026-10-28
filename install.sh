#!/usr/bin/env bash
# 安装依赖 + 生成漫画库配置。Linux / Android(DSHA 容器) 通用。
set -eu

BASE_DIR=$(cd "$(dirname "$0")" && pwd)
JM_ROOT=${JM_ROOT:-$HOME/JM_Downloads}

say() { printf '%s\n' "$*"; }

PY=$(command -v python3 || true)
[ -n "$PY" ] || { say "[x] 找不到 python3，请先安装 Python 3.9+"; exit 1; }
say "[+] Python: $PY ($($PY -V 2>&1))"

$PY - <<'PY' || { echo "[x] 需要 Python 3.9+"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

if [ "${JM_SKIP_DEPS:-0}" = "1" ]; then
  say "[=] 跳过依赖安装（JM_SKIP_DEPS=1）"
else
say "[*] 安装依赖（jmcomic / Pillow / pycryptodome / segno）"
if $PY -m pip --version >/dev/null 2>&1; then
  $PY -m pip install --quiet --disable-pip-version-check --break-system-packages \
    jmcomic Pillow pycryptodome segno
elif command -v uv >/dev/null 2>&1; then
  uv pip install --system --break-system-packages --quiet jmcomic Pillow pycryptodome segno
else
  say "[x] 既没有 pip 也没有 uv，请先安装 pip"
  exit 1
fi
fi

say "[+] 依赖装好了"
$PY - <<'PY'
import PIL, jmcomic, segno
from Crypto.Cipher import AES  # noqa: F401
print("[+] 自检通过: Pillow %s / jmcomic %s / segno %s" % (PIL.__version__, jmcomic.__version__, segno.__version__))
PY

say "[*] 准备漫画库目录 $JM_ROOT"
mkdir -p "$JM_ROOT"
OPT="$JM_ROOT/option.yml"
if [ -f "$OPT" ]; then
  say "[=] $OPT 已存在，保留不动"
else
  cat > "$OPT" <<YAML
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
  say "[+] 已写入 $OPT"
fi

chmod +x "$BASE_DIR/start-bot.sh" "$BASE_DIR"/autostart/*.sh 2>/dev/null || true

if [ -d "$HOME/.dsh/skills" ]; then
  say "[*] 检测到 DSH，把 skill 装到 ~/.dsh/skills/jm-weixin-bot"
  rm -rf "$HOME/.dsh/skills/jm-weixin-bot"
  cp -r "$BASE_DIR/skill" "$HOME/.dsh/skills/jm-weixin-bot"
fi

cat <<EOF

完成。下一步：

0) 先启用微信 clawbot 插件（首次必做）：
   微信升级到最新版 → 完全关掉微信 → 重新打开
   → 我 → 设置 → 插件，确认能看到「微信clawbot」。
   看不到就再杀一次微信进程、重新打开；还没有就确认微信是最新版。

1) 扫码绑定微信（只在第一次需要）：
   cd "$BASE_DIR/skill/scripts"
   python3 send_weixin.py --login-server --port 18791
   然后浏览器打开 http://127.0.0.1:18791/ ，用微信扫码。

2) 启动 bot：
   bash "$BASE_DIR/start-bot.sh"

3) 微信里给这个 bot 发：
   jm <id>     下载并回 PDF
   status      运行状态
   help        菜单

库目录：$JM_ROOT
EOF
