#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原生微信发送（腾讯 ilink bot 协议），不依赖 openclaw。

    python send_weixin.py --check                        # 看依赖与账号，不发消息
    python send_weixin.py --import-openclaw              # 一次性把 openclaw 的登录态搬到 DSH
    python send_weixin.py -m "你好"
    python send_weixin.py -m "JM 123 已完成" --media D:/AI/JM_Downloads/123/JM_123.pdf
    python send_weixin.py --media x.pdf --dry-run --json  # 只打印将要发送的 payload

凭证默认存在 ~/.dsh/weixin/accounts/<accountId>.json（含 token 与会话 context token），
首次用 --import-openclaw 从 ~/.openclaw/openclaw-weixin/accounts/ 复制，之后 openclaw 可以删掉。

协议来源：@tencent-weixin/openclaw-weixin 2.4.3 的 src/api/api.ts、src/cdn/*、src/messaging/send.ts。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

# 与插件 2.4.3 对齐的固定标识
ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = 132099  # 2.4.3 -> (2<<16)|(4<<8)|3
CHANNEL_VERSION = "2.4.3"
BOT_AGENT = "DSH/1.0"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_FILE = 4
ITEM_VIDEO = 5
MSG_BOT = 2
STATE_FINISH = 2

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

HOME = Path.home()
DSH_DIR = Path(os.environ.get("DSH_WEIXIN_DIR") or (HOME / ".dsh" / "weixin"))
OPENCLAW_DIR = Path(
    os.environ.get("OPENCLAW_WEIXIN_DIR") or (HOME / ".openclaw" / "openclaw-weixin" / "accounts")
)
ACCOUNTS_DIR = DSH_DIR / "accounts"


# ---------------------------------------------------------------- AES-128-ECB


def _aes_pycryptodome(data: bytes, key: bytes) -> bytes:
    from Crypto.Cipher import AES  # pycryptodome
    from Crypto.Util.Padding import pad

    # pycryptodome 的 encrypt() 不做填充，必须自己按 PKCS7 补齐到 16 字节
    return AES.new(key, AES.MODE_ECB).encrypt(pad(data, 16))


def _powershell_exe() -> str:
    import shutil

    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("找不到 PowerShell，无法用 .NET 兜底加密（装个 pycryptodome 更快: pip install pycryptodome）")


def _aes_dotnet(data: bytes, key: bytes) -> bytes:
    """没有 pycryptodome 时，用 Windows 自带的 .NET AES 兜底（ECB + PKCS7，兼容 PowerShell 5.1）。"""
    exe = _powershell_exe()
    with tempfile.TemporaryDirectory(prefix="dsh-aes-") as tmp:
        src = Path(tmp) / "in.bin"
        dst = Path(tmp) / "out.bin"
        src.write_bytes(data)
        key_hex = key.hex()
        script = (
            "$ErrorActionPreference='Stop';"
            "$a=[System.Security.Cryptography.Aes]::Create();"
            "$a.Mode=[System.Security.Cryptography.CipherMode]::ECB;"
            "$a.Padding=[System.Security.Cryptography.PaddingMode]::PKCS7;"
            "$h='%s'; $k=New-Object byte[] 16;"
            "for($i=0;$i -lt 16;$i++){$k[$i]=[Convert]::ToByte($h.Substring($i*2,2),16)};"
            "$a.Key=$k;"
            "$in=[System.IO.File]::ReadAllBytes('%s');"
            "$enc=$a.CreateEncryptor().TransformFinalBlock($in,0,$in.Length);"
            "[System.IO.File]::WriteAllBytes('%s',$enc);"
        ) % (key_hex, src, dst)
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("AES 加密失败（.NET 兜底）: " + (proc.stderr or "")[:300])
        return dst.read_bytes()


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if os.environ.get("WEIXIN_FORCE_DOTNET_AES") == "1":
        return _aes_dotnet(data, key)
    try:
        return _aes_pycryptodome(data, key)
    except ImportError:
        return _aes_dotnet(data, key)


def padded_size(size: int) -> int:
    """AES-128-ECB + PKCS7 后的密文长度。"""
    return (size // 16 + 1) * 16


# ------------------------------------------------------------------- HTTP


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _headers(token: str | None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _read_json(req, timeout: float, url: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %s %s: %s" % (err.code, url, body[:300])) from None
    except urllib.error.URLError as err:
        raise RuntimeError("连不上 %s: %s" % (url, err.reason)) from None
    except (TimeoutError, OSError) as err:
        raise RuntimeError("请求 %s 超时/中断: %s" % (url, err)) from None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("响应不是 JSON: %s" % raw[:200]) from None


def post_json(url: str, payload: dict, token: str | None, timeout: float = 30.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_headers(token), method="POST")
    return _read_json(req, timeout, url)


def get_json(url: str, token: str | None = None, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers=_headers(token), method="GET")
    return _read_json(req, timeout, url)


def upload_ciphertext(url: str, ciphertext: bytes, retries: int = 3, timeout: float = 300.0) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                param = resp.headers.get("x-encrypted-param")
                if not param:
                    raise RuntimeError("CDN 响应缺少 x-encrypted-param 头")
                return param
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            last = RuntimeError("CDN 上传 HTTP %s: %s" % (err.code, body[:200]))
            if 400 <= err.code < 500:
                raise last from None
            time.sleep(1.5 * attempt)
        except Exception as err:  # noqa: BLE001 - 网络抖动统一重试
            last = err if isinstance(err, Exception) else RuntimeError(str(err))
            time.sleep(1.5 * attempt)
    raise last or RuntimeError("CDN 上传失败")


# ------------------------------------------------------------- 凭证存储


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def import_from_openclaw() -> list[dict]:
    if not OPENCLAW_DIR.is_dir():
        raise SystemExit("找不到 openclaw 微信账号目录: %s" % OPENCLAW_DIR)
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    imported = []
    for path in sorted(OPENCLAW_DIR.glob("*.json")):
        if path.name.endswith(".context-tokens.json") or path.name.endswith(".sync.json"):
            continue
        account = json.loads(path.read_text(encoding="utf-8"))
        account_id = path.stem
        ctx_file = OPENCLAW_DIR / (account_id + ".context-tokens.json")
        tokens = {}
        updated = None
        if ctx_file.exists():
            tokens = json.loads(ctx_file.read_text(encoding="utf-8"))
            updated = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ctx_file.stat().st_mtime))
        sync_file = OPENCLAW_DIR / (account_id + ".sync.json")
        sync_buf = ""
        if sync_file.exists():
            sync_buf = json.loads(sync_file.read_text(encoding="utf-8")).get("get_updates_buf", "")
        record = {
            "accountId": account_id,
            "token": account.get("token"),
            "baseUrl": account.get("baseUrl") or DEFAULT_BASE_URL,
            "userId": account.get("userId"),
            "contextTokens": tokens,
            "contextUpdatedAt": updated,
            "syncBuf": sync_buf,
            "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "openclaw:" + str(path),
        }
        _write_json(ACCOUNTS_DIR / (account_id + ".json"), record)
        imported.append(record)
    return imported


def load_accounts() -> dict[str, dict]:
    accounts: dict[str, dict] = {}
    if not ACCOUNTS_DIR.is_dir():
        return accounts
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        account_id = record.get("accountId") or path.stem
        record["accountId"] = account_id
        accounts[account_id] = record
    return accounts


def pick_account(accounts: dict[str, dict], wanted: str | None) -> dict:
    if not accounts:
        raise SystemExit(
            "还没有凭证。先跑一次 --import-openclaw 从 openclaw 复制，或手工放一个账号文件到 %s"
            % ACCOUNTS_DIR
        )
    if wanted:
        if wanted in accounts:
            return accounts[wanted]
        matches = [name for name in accounts if name.startswith(wanted)]
        if len(matches) == 1:
            return accounts[matches[0]]
        raise SystemExit("账号 %s 不存在，可选: %s" % (wanted, ", ".join(sorted(accounts))))
    if len(accounts) == 1:
        return next(iter(accounts.values()))
    ranked = sorted(
        accounts.values(),
        key=lambda rec: (rec.get("contextUpdatedAt") or "", rec.get("importedAt") or ""),
        reverse=True,
    )
    return ranked[0]


def pick_peer(account: dict, wanted: str | None, dry_run: bool) -> str:
    if wanted:
        return wanted
    peer = account.get("userId")
    if peer:
        return peer
    peers = list((account.get("contextTokens") or {}).keys())
    if len(peers) == 1:
        return peers[0]
    if not peers:
        raise SystemExit("账号里没有 userId 也没有会话记录，请用 --to 指定接收人")
    if dry_run:
        return peers[0]
    raise SystemExit("有多个接收人可选，请用 --to 指定: %s" % ", ".join(peers))


# -------------------------------------------------------------- 扫码登录

ILINK_BOT_TYPE = "3"
QR_POLL_TIMEOUT = 35.0


def _render_qr(text: str, out_path: Path | None = None) -> str | None:
    """把二维码内容渲染成 PNG；没有 segno 时返回 None（调用方给出 URL 兜底）。"""
    try:
        import segno
    except ImportError:
        return None
    path = Path(out_path) if out_path else (DSH_DIR / "login-qr.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    segno.make(text, error="m").save(str(path), scale=6, border=2)
    return str(path)


def fetch_login_qr(bot_type: str = ILINK_BOT_TYPE) -> dict:
    tokens = [rec.get("token") for rec in load_accounts().values() if rec.get("token")]
    return post_json(
        DEFAULT_BASE_URL + "/ilink/bot/get_bot_qrcode?bot_type=" + urllib.parse.quote(str(bot_type)),
        {"local_token_list": tokens},
        None,
        timeout=20,
    )


def poll_login_status(
    qrcode: str,
    base_url: str = DEFAULT_BASE_URL,
    verify_code: str | None = None,
    timeout: float = QR_POLL_TIMEOUT,
) -> dict:
    url = base_url.rstrip("/") + "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(qrcode)
    if verify_code:
        url += "&verify_code=" + urllib.parse.quote(str(verify_code))
    return get_json(url, None, timeout=timeout)


def save_login(account_id: str, token: str, base_url: str | None, user_id: str | None) -> dict:
    record = {
        "accountId": account_id,
        "token": token,
        "baseUrl": (base_url or DEFAULT_BASE_URL).rstrip("/"),
        "userId": user_id,
        "contextTokens": {},
        "contextUpdatedAt": None,
        "syncBuf": "",
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "qr-login",
    }
    _write_json(ACCOUNTS_DIR / (account_id + ".json"), record)
    return record


def qr_login(
    bot_type: str = ILINK_BOT_TYPE,
    qrcode: str | None = None,
    verify_code: str | None = None,
    qr_out: str | None = None,
    timeout: float = 240.0,
) -> dict:
    """两步式扫码登录：先取二维码（返回 png/url），再带 --qrcode 轮询结果。"""
    qr_url = None
    if not qrcode:
        resp = fetch_login_qr(bot_type)
        qrcode = resp.get("qrcode")
        qr_url = resp.get("qrcode_img_content")
        if not qrcode or not qr_url:
            return {
                "ok": False,
                "status": "error",
                "error": "get_bot_qrcode 返回不完整: %s" % json.dumps(resp, ensure_ascii=False)[:200],
            }
        png = _render_qr(qr_url, Path(qr_out) if qr_out else None)
        return {
            "ok": True,
            "status": "qr",
            "qrcode": qrcode,
            "url": qr_url,
            "png": png,
            "hint": "把 png 给用户用手机微信扫（没有 png 就打开 url），扫完带同一个 --qrcode 再跑一次轮询",
        }

    base = DEFAULT_BASE_URL
    deadline = time.time() + max(timeout, 5.0)
    while time.time() < deadline:
        try:
            status_resp = poll_login_status(qrcode, base, verify_code)
        except RuntimeError:
            status_resp = {"status": "wait"}
        status = status_resp.get("status")

        if status == "wait":
            time.sleep(1.0)
            continue
        if status == "scaned":
            time.sleep(1.0)
            continue
        if status == "scaned_but_redirect":
            host = status_resp.get("redirect_host")
            if host:
                base = "https://" + host
            continue
        if status == "need_verifycode":
            return {
                "ok": False,
                "status": "need_verifycode",
                "qrcode": qrcode,
                "hint": "手机微信上显示了一组数字，问用户要过来，然后带 --verify-code <数字> --qrcode %s 重跑" % qrcode,
            }
        if status == "verify_code_blocked":
            return {
                "ok": False,
                "status": "verify_code_blocked",
                "qrcode": qrcode,
                "hint": "验证码错太多次，等一会儿重新 --login 取新二维码",
            }
        if status == "expired":
            return {
                "ok": False,
                "status": "expired",
                "qrcode": qrcode,
                "hint": "二维码过期了，重新跑 --login 取一张新的",
            }
        if status == "binded_redirect":
            return {
                "ok": True,
                "status": "binded_redirect",
                "alreadyConnected": True,
                "hint": "这个微信已经绑过本机的 bot，无需重复登录",
            }
        if status == "confirmed":
            account_id = status_resp.get("ilink_bot_id")
            token = status_resp.get("bot_token")
            if not account_id or not token:
                return {
                    "ok": False,
                    "status": "error",
                    "error": "登录已确认但缺少 bot_token/ilink_bot_id: %s"
                    % json.dumps(status_resp, ensure_ascii=False)[:200],
                }
            record = save_login(account_id, token, status_resp.get("baseurl"), status_resp.get("ilink_user_id"))
            return {
                "ok": True,
                "status": "confirmed",
                "accountId": account_id,
                "userId": record.get("userId"),
                "baseUrl": record.get("baseUrl"),
                "store": str(ACCOUNTS_DIR / (account_id + ".json")),
                "hint": "登录完成；让用户在微信里给这个 bot 发条消息，再用 --sync 建立会话",
            }
        time.sleep(1.0)

    return {
        "ok": False,
        "status": "wait",
        "qrcode": qrcode,
        "hint": "还没扫/还没确认，稍后带同一个 --qrcode 继续轮询",
    }


def _qr_png_bytes(text: str) -> bytes | None:
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>微信扫码登录</title>
<style>
 body{font-family:system-ui,"Segoe UI",sans-serif;background:#0f1115;color:#e6e6e6;display:flex;
      flex-direction:column;align-items:center;padding:28px;margin:0}
 .card{background:#171a21;border-radius:16px;padding:22px 26px;box-shadow:0 8px 30px rgba(0,0,0,.4);text-align:center}
 img{width:300px;height:300px;image-rendering:pixelated;background:#fff;border-radius:8px;padding:8px}
 #st{margin-top:14px;font-size:15px;color:#9ad}
 #sub{margin-top:6px;font-size:12px;color:#777}
 .row{margin-top:12px;display:none}
 input{width:120px;padding:6px 8px;border-radius:8px;border:1px solid #333;background:#0f1115;color:#eee}
 button{padding:6px 12px;border-radius:8px;border:0;background:#3b82f6;color:#fff;cursor:pointer}
</style></head>
<body><div class="card">
 <div id="st">正在生成二维码…</div>
 <img id="qr" src="/qr.png" alt="二维码">
 <div class="row" id="vrow"><input id="code" placeholder="配对数字"><button onclick="verify()">提交</button></div>
 <div id="sub">二维码自动刷新，请用手机微信扫描</div>
</div>
<script>
const MAP={init:"正在生成二维码…",wait:"等待扫码…",scaned:"已扫码，请在手机上确认",
 expired:"二维码已过期，正在换新…",need_verifycode:"请输入手机微信上显示的数字",
 confirmed:"✅ 登录成功，可以关掉本页了",binded_redirect:"✅ 这个微信已经绑定过，无需重复登录",
 error:"出错了"};
async function tick(){
 try{
  const r=await fetch('/status',{cache:'no-store'});const j=await r.json();
  document.getElementById('st').textContent=MAP[j.status]||j.status;
  document.getElementById('vrow').style.display=j.status==='need_verifycode'?'block':'none';
  if(j.status==='confirmed'||j.status==='binded_redirect'){clearInterval(t);}
  if(j.error){document.getElementById('sub').textContent=j.error;}
 }catch(e){}
}
async function verify(){
 const c=document.getElementById('code').value.trim(); if(!c)return;
 await fetch('/verify?code='+encodeURIComponent(c),{cache:'no-store'}); tick();
}
setInterval(()=>{document.getElementById('qr').src='/qr.png?t='+Date.now();},15000);
setInterval(tick,2500); tick();
</script></body></html>"""


def run_login_server(
    host: str = "127.0.0.1",
    port: int = 18791,
    bot_type: str = ILINK_BOT_TYPE,
    timeout: float = 900.0,
) -> int:
    """起一个本地页面，自动换新二维码，扫码/配对码都在浏览器里完成。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {
        "qrcode": None,
        "url": None,
        "created": 0.0,
        "status": "init",
        "account": None,
        "error": None,
        "done": False,
        "verify_code": None,
    }
    lock = threading.Lock()
    qr_quiet = 75.0

    def refresh_qr(force: bool = False) -> None:
        with lock:
            fresh = state["qrcode"] and (time.time() - state["created"]) <= qr_quiet
            if fresh and not force:
                return
        resp = fetch_login_qr(bot_type)
        qr = resp.get("qrcode")
        url = resp.get("qrcode_img_content")
        if not qr or not url:
            raise RuntimeError("get_bot_qrcode 返回不完整")
        with lock:
            state.update(qrcode=qr, url=url, created=time.time(), status="wait", verify_code=None)

    def poller() -> None:
        while True:
            with lock:
                if state["done"]:
                    return
                qr = state["qrcode"]
                code = state["verify_code"]
            if not qr:
                time.sleep(0.3)
                continue
            try:
                st = poll_login_status(qr, verify_code=code, timeout=30)
            except Exception:
                time.sleep(1.0)
                continue
            status = st.get("status")
            if status == "scaned":
                with lock:
                    state["status"] = "scaned"
                time.sleep(0.4)
                continue
            if status in (None, "wait", "scaned_but_redirect"):
                time.sleep(0.4)
                continue
            if status == "need_verifycode":
                with lock:
                    state["status"] = "need_verifycode"
                time.sleep(1.5)
                continue
            if status == "verify_code_blocked":
                with lock:
                    state["status"] = "need_verifycode"
                    state["error"] = "验证码错误次数过多，已换新二维码，请重新扫"
                try:
                    refresh_qr(force=True)
                except Exception:
                    time.sleep(1.0)
                continue
            if status == "expired":
                with lock:
                    state["status"] = "expired"
                try:
                    refresh_qr(force=True)
                except Exception:
                    time.sleep(1.0)
                continue
            if status == "binded_redirect":
                with lock:
                    state["status"] = "binded_redirect"
                    state["done"] = True
                return
            if status == "confirmed":
                account_id = st.get("ilink_bot_id")
                token = st.get("bot_token")
                if not account_id or not token:
                    with lock:
                        state["status"] = "error"
                        state["error"] = "confirmed 但缺少 bot_token/ilink_bot_id"
                        state["done"] = True
                    return
                record = save_login(account_id, token, st.get("baseurl"), st.get("ilink_user_id"))
                with lock:
                    state["status"] = "confirmed"
                    state["account"] = {
                        "accountId": account_id,
                        "userId": record.get("userId"),
                        "baseUrl": record.get("baseUrl"),
                    }
                    state["done"] = True
                return
            time.sleep(1.0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/status":
                with lock:
                    payload = {
                        "status": state["status"],
                        "account": state["account"],
                        "error": state["error"],
                        "qrAgeSec": round(time.time() - state["created"], 1) if state["created"] else None,
                    }
                return self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if path == "/verify":
                query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                code = (query.get("code") or [""])[0].strip()
                if code:
                    with lock:
                        state["verify_code"] = code
                return self._send(200, b'{"ok":true}', "application/json")
            if path == "/qr.png":
                try:
                    refresh_qr()
                except Exception as err:
                    return self._send(500, str(err).encode("utf-8"), "text/plain; charset=utf-8")
                with lock:
                    url = state["url"]
                png = _qr_png_bytes(url) if url else None
                if png is None:
                    return self._send(500, "缺少 segno：python -m pip install segno".encode("utf-8"), "text/plain; charset=utf-8")
                return self._send(200, png, "image/png")
            return self._send(200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")

    try:
        refresh_qr(force=True)
    except Exception:
        pass

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        server = ThreadingHTTPServer((host, port + 1), Handler)
        port += 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    print(json.dumps({"status": "listening", "url": "http://%s:%d/" % (host, port)}, ensure_ascii=False), flush=True)

    deadline = time.time() + max(timeout, 30.0)
    while time.time() < deadline:
        with lock:
            if state["done"]:
                break
        time.sleep(0.5)

    with lock:
        result = {
            "ok": state["status"] in ("confirmed", "binded_redirect"),
            "status": state["status"],
            "account": state["account"],
            "error": state["error"],
            "store": str(ACCOUNTS_DIR),
        }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    server.shutdown()
    return 0


# ------------------------------------------------------------------ 发送


def _client_id() -> str:
    return "dsh-weixin:%d-%s" % (int(time.time() * 1000), secrets.token_hex(4))


def fetch_updates(account: dict, timeout: float = 8.0) -> list[dict]:
    """拉一次 getUpdates；服务端长轮询没消息时按超时当作空结果。"""
    base = (account.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    payload = {
        "get_updates_buf": account.get("syncBuf") or "",
        "base_info": _base_info(),
    }
    try:
        resp = post_json(base + "/ilink/bot/getupdates", payload, account["token"], timeout=timeout)
    except Exception:  # noqa: BLE001 - 长轮询超时属于正常“没有新消息”
        return []
    if resp.get("get_updates_buf"):
        account["syncBuf"] = resp["get_updates_buf"]
    return resp.get("msgs") or []


def sync_context(account: dict, peer: str | None = None, timeout: float = 8.0) -> bool:
    """拉一次新消息，把里面的 context_token 存下来（会话窗口的入场券）。返回是否拿到新 token。"""
    msgs = fetch_updates(account, timeout=timeout)
    tokens = account.setdefault("contextTokens", {})
    before = dict(tokens)
    for msg in msgs:
        token = msg.get("context_token")
        from_id = msg.get("from_user_id")
        if token and from_id:
            tokens[from_id] = token
    changed = tokens != before
    if msgs:
        account["contextUpdatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_json(ACCOUNTS_DIR / (account["accountId"] + ".json"), account)
    if changed:
        return True
    target = peer or account.get("userId")
    return bool(target and tokens.get(target) and tokens.get(target) != before.get(target))


def _is_session_error(err: Exception) -> bool:
    text = str(err)
    return "ret=-2" in text or "ret=-14" in text


def _send_items(account: dict, peer: str, items: list[dict], dry_run: bool, use_context: bool) -> list[dict]:
    token = account.get("token") or ""
    context_token = (account.get("contextTokens") or {}).get(peer) if use_context else None
    payloads = []
    for item in items:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": peer,
                "client_id": _client_id(),
                "message_type": MSG_BOT,
                "message_state": STATE_FINISH,
                "item_list": [item],
            },
            "base_info": _base_info(),
        }
        if context_token:
            body["msg"]["context_token"] = context_token
        payloads.append(body)

    if dry_run:
        return payloads

    base = (account.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")
    url = base + "/ilink/bot/sendmessage"
    for body in payloads:
        try:
            _post_message(url, body, token)
            continue
        except RuntimeError as err:
            if not _is_session_error(err):
                raise
            first_error = err

        # 会话窗口失效：拉一次更新拿新的 context_token 再试
        sync_context(account, peer, timeout=8.0)
        fresh = (account.get("contextTokens") or {}).get(peer)
        if not fresh:
            raise RuntimeError(
                "发送被拒：%s；也没拉到新的微信消息。请先在微信里给这个 bot 发一句话再重试。" % first_error
            )
        body["msg"]["context_token"] = fresh
        body["msg"]["client_id"] = _client_id()
        _post_message(url, body, token)
    return payloads


def _post_message(url: str, body: dict, token: str) -> None:
    resp = post_json(url, body, token)
    if isinstance(resp, dict):
        errcode = resp.get("errcode") or resp.get("ret")
        if errcode not in (None, 0):
            hint = ""
            if str(errcode) == "-2":
                hint = "（服务端 prepare failed：通常是没有活跃会话，先在微信里给这个 bot 发一条消息，再立即重试）"
            elif str(errcode) == "-14":
                hint = "（会话过期，先在微信里给 bot 发条消息刷新 context_token 再试）"
            raise RuntimeError(
                "发送被拒 ret=%s errmsg=%s%s" % (errcode, resp.get("errmsg"), hint)
            )


def build_media_item(account: dict, peer: str, path: Path, dry_run: bool) -> dict:
    data = path.read_bytes()
    rawsize = len(data)
    raw_md5 = hashlib.md5(data).hexdigest()
    filesize = padded_size(rawsize)
    suffix = path.suffix.lower()

    if suffix in IMAGE_SUFFIXES:
        media_type, item_type, item_key = MEDIA_IMAGE, ITEM_IMAGE, "image_item"
    elif suffix in VIDEO_SUFFIXES:
        media_type, item_type, item_key = MEDIA_VIDEO, ITEM_VIDEO, "video_item"
    else:
        media_type, item_type, item_key = MEDIA_FILE, ITEM_FILE, "file_item"

    filekey = secrets.token_hex(16)
    aeskey = secrets.token_bytes(16)
    token = account.get("token") or ""
    base = (account.get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")

    upload_req = {
        "filekey": filekey,
        "media_type": media_type,
        "to_user_id": peer,
        "rawsize": rawsize,
        "rawfilemd5": raw_md5,
        "filesize": filesize,
        "no_need_thumb": True,
        "aeskey": aeskey.hex(),
        "base_info": _base_info(),
    }

    if dry_run:
        return {
            "_dry_run": True,
            "upload_request": upload_req,
            "upload_bytes": filesize,
            "file": str(path),
            "media_type": media_type,
        }

    upload_resp = post_json(base + "/ilink/bot/getuploadurl", upload_req, token)
    upload_url = (upload_resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = upload_resp.get("upload_param")
        if not param:
            raise RuntimeError("getuploadurl 没返回上传地址: %s" % json.dumps(upload_resp)[:300])
        upload_url = "%s/upload?encrypted_query_param=%s&filekey=%s" % (
            DEFAULT_CDN_BASE,
            urllib.parse.quote(param),
            urllib.parse.quote(filekey),
        )

    encrypted_param = upload_ciphertext(upload_url, aes_ecb_encrypt(data, aeskey))

    media = {
        "encrypt_query_param": encrypted_param,
        "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),
        "encrypt_type": 1,
    }

    if item_type == ITEM_IMAGE:
        item = {
            "type": ITEM_IMAGE,
            "image_item": {"media": media, "mid_size": filesize},
        }
    elif item_type == ITEM_VIDEO:
        item = {
            "type": ITEM_VIDEO,
            "video_item": {"media": media, "video_size": filesize},
        }
    else:
        item = {
            "type": ITEM_FILE,
            "file_item": {"media": media, "file_name": path.name, "len": str(rawsize)},
        }
    return item


# -------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="原生微信发送（ilink bot 协议，不依赖 openclaw）")
    parser.add_argument("--account", help="账号 id 或前缀，默认按最近会话自动选")
    parser.add_argument("--to", help="接收人 openid，默认发给自己（账号的 userId）")
    parser.add_argument("-m", "--message", help="文本内容")
    parser.add_argument("--media", help="附件路径（PDF 等按文件发送，图片按图片发送）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 payload，不发网络请求")
    parser.add_argument("--no-context", action="store_true", help="不带上存下来的会话 context_token（一般别用，没有 context 发不出去）")
    parser.add_argument("--sync", action="store_true", help="先拉一次新消息刷新 context_token，再发送")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--login", action="store_true", help="扫码登录：不带 --qrcode 时取二维码，带 --qrcode 时轮询结果")
    parser.add_argument("--login-server", action="store_true", help="起本地扫码页面，二维码会自动换新")
    parser.add_argument("--port", type=int, default=18791, help="扫码页面端口，默认 18791")
    parser.add_argument("--bot-type", default=ILINK_BOT_TYPE, help="二维码 bot_type，默认 %s" % ILINK_BOT_TYPE)
    parser.add_argument("--qrcode", help="继续轮询这张二维码")
    parser.add_argument("--verify-code", help="手机微信上显示的配对数字")
    parser.add_argument("--qr-out", help="二维码 PNG 保存路径")
    parser.add_argument("--login-timeout", type=float, default=240.0, help="登录轮询总超时秒数")
    parser.add_argument("--import-openclaw", action="store_true", help="从 openclaw 复制登录态后退出")
    parser.add_argument("--check", action="store_true", help="打印依赖与账号状态后退出")
    parser.add_argument("--timeout", type=float, default=120.0, help="单次请求超时秒数，默认 120")
    args = parser.parse_args(argv)

    if args.import_openclaw:
        imported = import_from_openclaw()
        result = {
            "imported": [
                {
                    "accountId": rec["accountId"],
                    "userId": rec["userId"],
                    "baseUrl": rec["baseUrl"],
                    "contextPeers": list((rec.get("contextTokens") or {}).keys()),
                }
                for rec in imported
            ],
            "store": str(ACCOUNTS_DIR),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.login_server:
        return run_login_server(
            port=args.port,
            bot_type=args.bot_type,
            timeout=args.login_timeout,
        )

    if args.login:
        result = qr_login(
            bot_type=args.bot_type,
            qrcode=args.qrcode,
            verify_code=args.verify_code,
            qr_out=args.qr_out,
            timeout=args.login_timeout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    accounts = load_accounts()
    account = pick_account(accounts, args.account)

    if args.check:
        try:
            import Crypto  # noqa: F401

            aes_backend = "pycryptodome"
        except ImportError:
            aes_backend = ".NET (pwsh)"
        result = {
            "aes": aes_backend,
            "store": str(ACCOUNTS_DIR),
            "accountId": account["accountId"],
            "baseUrl": account.get("baseUrl"),
            "userId": account.get("userId"),
            "tokenLength": len(account.get("token") or ""),
            "contextPeers": list((account.get("contextTokens") or {}).keys()),
            "accounts": sorted(accounts),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not args.message and not args.media and not args.sync:
        parser.error("至少给 --message 或 --media（或者 --sync 只刷新会话）")

    peer = pick_peer(account, args.to, args.dry_run)

    if args.sync:
        before = (account.get("contextTokens") or {}).get(peer)
        sync_context(account, peer, timeout=min(args.timeout, 15.0))
        after = (account.get("contextTokens") or {}).get(peer)
        if not args.message and not args.media:
            result = {
                "accountId": account["accountId"],
                "to": peer,
                "contextToken": bool(after),
                "refreshed": bool(after) and after != before,
                "syncBuf": bool(account.get("syncBuf")),
                "store": str(ACCOUNTS_DIR),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

    items: list[dict] = []
    if args.message:
        items.append({"type": ITEM_TEXT, "text_item": {"text": args.message}})

    media_path = Path(args.media) if args.media else None
    media_item = None
    if media_path is not None:
        if not media_path.is_file():
            raise SystemExit("附件不存在: %s" % media_path)
        media_item = build_media_item(account, peer, media_path, args.dry_run)
        if not args.dry_run:
            items.append(media_item)

    payloads = _send_items(account, peer, items, args.dry_run, not args.no_context) if items else []

    if args.dry_run:
        result = {
            "dryRun": True,
            "accountId": account["accountId"],
            "to": peer,
            "contextToken": bool((account.get("contextTokens") or {}).get(peer)) and not args.no_context,
            "textPayloads": payloads,
            "media": media_item,
        }
    else:
        result = {
            "ok": True,
            "accountId": account["accountId"],
            "to": peer,
            "messages": len(payloads),
            "media": str(media_path) if media_path else None,
            "messageIds": [body["msg"]["client_id"] for body in payloads],
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as err:
        print(json.dumps({"ok": False, "error": str(err)}, ensure_ascii=False))
        sys.exit(1)
