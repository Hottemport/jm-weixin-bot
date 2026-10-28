#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信命令桥：只认 jm <id> / help / status，不调用任何模型。

    python weixin_bot.py --selftest        # 自检：不联网、不发送，打印解析结果
    python weixin_bot.py --once            # 处理当前积压的消息后退出
    python weixin_bot.py --serve           # 常驻，长轮询微信（可挂登录自启）

收到 "jm 123" 时：本地 jmcomic 下载 → merge_pdf 合并 → 用这条消息自带的
context_token 把 PDF 发回去，因此不会遇到 -2 prepare failed。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import send_weixin as wx  # noqa: E402

DEFAULT_ROOT = Path(os.environ.get("JM_ROOT") or r"D:\AI\JM_Downloads")
STATE_PATH = Path(os.environ.get("JM_BOT_STATE") or (Path.home() / ".dsh" / "weixin" / "bot-state.json"))
LOG_PATH = Path(os.environ.get("JM_BOT_LOG") or (Path.home() / ".dsh" / "weixin" / "bot.log"))

JM_RE = re.compile(r"^(?:jm|ＪＭ)\s*[-_#]?\s*(\d{3,9})$", re.IGNORECASE)
DOWNLOAD_RE = re.compile(r"^(?:下载|download)\s*[-_#]?\s*(\d{3,9})$", re.IGNORECASE)

HELP_TEXT = (
    "可用命令：\n"
    "jm <id>     下载并回 PDF（例：jm1088227）\n"
    "status      运行状态\n"
    "help        这张菜单\n"
    "只认这三条，闲聊不会回。"
)

SKIP_OLDER_THAN_MS = 60 * 60 * 1000  # 超过 1 小时的积压消息不回，避免重启后刷屏


# ------------------------------------------------------------------ 基础设施


def log(message: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "lastJm": None}


def save_state(state: dict) -> None:
    state["seen"] = state.get("seen", [])[-200:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def persist_account(account: dict) -> None:
    wx._write_json(wx.ACCOUNTS_DIR / (account["accountId"] + ".json"), account)


def acquire_lock(max_wait: float = 3.0):
    """单实例锁：文件锁随进程退出自动释放，避免自启和手动启动重复跑。"""
    try:
        import msvcrt
    except ImportError:
        return True
    lock_path = STATE_PATH.parent / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    deadline = time.time() + max_wait
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            return handle
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            time.sleep(0.3)


def rotate_log(max_bytes: int = 5 * 1024 * 1024) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > max_bytes:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
    except OSError:
        pass


def extract_text(msg: dict) -> str:
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            text = ((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                return text
    return ""


def message_key(msg: dict) -> str:
    if msg.get("message_id"):
        return "mid:" + str(msg["message_id"])
    if msg.get("seq"):
        return "seq:" + str(msg["seq"])
    return "raw:%s:%s" % (msg.get("from_user_id"), msg.get("create_time_ms"))


def is_allowed(msg: dict, allow: set[str]) -> bool:
    sender = (msg.get("from_user_id") or "").strip()
    return bool(sender) and sender in allow


# ---------------------------------------------------------------------- 回复


def reply(account: dict, peer: str, context_token: str | None, text: str, media: Path | None = None) -> None:
    if context_token:
        account.setdefault("contextTokens", {})[peer] = context_token
    items = []
    if text:
        items.append({"type": wx.ITEM_TEXT, "text_item": {"text": text}})
    if media is not None and media.is_file():
        items.append(wx.build_media_item(account, peer, media, dry_run=False))
    wx._send_items(account, peer, items, dry_run=False, use_context=True)


def notify_ready(account: dict, allow: set[str]) -> bool:
    """启动后给主人发一条「已就绪」。发得出去说明连接、凭证、发送链路都通了。"""
    if os.environ.get("JM_READY_NOTIFY", "1") == "0":
        log("就绪通知已关闭（JM_READY_NOTIFY=0）")
        return False
    peer = account.get("userId") or next(iter(allow), None)
    if not peer:
        log("就绪通知跳过：账号里没有 userId")
        return False
    text = "✅ JM bot 已就绪（%s 启动），发 jm <id> 开始下载。" % time.strftime("%H:%M")
    base = (account.get("baseUrl") or wx.DEFAULT_BASE_URL).rstrip("/")
    token = account.get("token") or ""
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": peer,
            "client_id": wx._client_id(),
            "message_type": wx.MSG_BOT,
            "message_state": wx.STATE_FINISH,
            "item_list": [{"type": wx.ITEM_TEXT, "text_item": {"text": text}}],
        },
        "base_info": wx._base_info(),
    }
    # 故意不用 _send_items：它在 -2 时会顺手 getUpdates 刷新 context，
    # 可能把用户刚发的命令吃掉。通知发不出去就只记日志，不抢消息。
    for attempt in range(1, 4):
        try:
            wx._post_message(base + "/ilink/bot/sendmessage", body, token)
            log("已发送就绪通知 -> %s" % peer)
            return True
        except Exception as err:  # noqa: BLE001 - 通知失败不该影响接单
            if attempt < 3:
                time.sleep(20)
            else:
                log("就绪通知没发出去: %r" % (err,))
    return False


# ---------------------------------------------------------------------- 任务


def find_pdf(root: Path, jm_id: str) -> Path:
    return root / jm_id / ("JM_%s.pdf" % jm_id)


def run_cmd(cmd: list[str], timeout: float) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-1500:]
    except subprocess.TimeoutExpired:
        return 124, "超时 %ss" % int(timeout)
    except FileNotFoundError as err:
        return 127, str(err)


def _download_error(root: Path) -> str:
    """从 jmcomic 的下载日志里捞最后一条异常，给微信回一句人话。"""
    log_path = root / "jmcomic-download.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "jmcomic 没有留下下载日志"
    for line in reversed(lines[-100:]):
        text = line.strip()
        if "MissingAlbum" in text or "请求的本子不存在" in text:
            return text[:200]
        if text.startswith("jmcomic.") and "Exception" in text:
            return text[:200]
    return "jmcomic 没有生成相册目录"


PROGRESS_AFTER = float(os.environ.get("JM_PROGRESS_AFTER", "180"))
PROGRESS_EVERY = float(os.environ.get("JM_PROGRESS_EVERY", "180"))

IMAGE_TO_REPORT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def count_images(folder: Path) -> int:
    try:
        return sum(1 for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_TO_REPORT)
    except OSError:
        return 0


def _run_jmcomic(cmd: list[str], timeout: float, root: Path, jm_id: str, progress=None) -> tuple[int, str]:
    """跑 jmcomic 并轮询：超过 PROGRESS_AFTER 秒还没完，就每 PROGRESS_EVERY 秒报一次进度。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    started = last = time.time()
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, text=True, creationflags=flags)
        except FileNotFoundError as err:
            return 127, str(err)
        while True:
            try:
                proc.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                now = time.time()
                if progress and now - started >= PROGRESS_AFTER and now - last >= PROGRESS_EVERY:
                    try:
                        progress("⏳ JM %s 还在下：已落 %d 页，已用 %.0f 分钟" % (
                            jm_id, count_images(root / jm_id), (now - started) / 60))
                    except Exception as err:  # noqa: BLE001 - 进度发不出去不影响下载
                        log("进度消息发送失败: %r" % (err,))
                    last = now
                if now - started > timeout:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    return 124, "超时 %ss" % int(timeout)
        out.seek(0)
        return proc.returncode, out.read()[-1500:]
    finally:
        out.close()


def download_jm(jm_id: str, root: Path, timeout: float = 1800.0, progress=None) -> tuple[bool, str, Path | None]:
    pdf = find_pdf(root, jm_id)
    force = False
    if pdf.is_file():
        size = pdf.stat().st_size
        if size >= 1024:
            return True, "之前下过（%.1f MB），直接发你" % (size / 1048576), pdf
        log("已有 PDF 只有 %d 字节，判定损坏，重新合并" % size)
        force = True

    option = root / "option.yml"
    if not option.is_file():
        return False, "缺少下载配置 %s" % option, None

    jmcomic = shutil.which("jmcomic")
    cmd = [jmcomic] if jmcomic else [sys.executable, "-m", "jmcomic"]
    cmd += [jm_id, "--option=%s" % option]
    code, output = _run_jmcomic(cmd, timeout, root, jm_id, progress)
    if code != 0 and not find_pdf(root, jm_id).is_file():
        # jmcomic 常以“部分失败”退出，但图片可能已经落地，合并阶段再判断
        log("jmcomic 退出码 %s: %s" % (code, output[-300:].replace("\n", " ")))

    # jmcomic 遇到“本子不存在/不可见”时退出码仍然是 0，只会往下载日志里写异常，
    # 所以先看相册目录有没有落地，避免把「ID 错了」误报成「合并失败：目录不存在」
    if not (root / jm_id).is_dir():
        detail = _download_error(root)
        if "不存在" in detail or "MissingAlbum" in detail:
            return False, "JM %s 不存在或不可见（id 可能有误）" % jm_id, None
        return False, "JM %s 下载失败：%s" % (jm_id, detail), None

    merge = [sys.executable, str(SCRIPT_DIR / "merge_pdf.py"), "--id", jm_id, "--root", str(root)]
    if force:
        merge.append("--force")
    code, output = run_cmd(merge, 900)
    try:
        data = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, "合并失败：%s" % output[-300:], None
    if data.get("error"):
        return False, "合并失败：%s" % data["error"], None
    if not Path(data.get("pdf", "")).is_file():
        return False, "合并后没找到 PDF", None
    return True, "下载完成：%s 页，%.1f MB" % (data.get("pages", 0), int(data.get("bytes", 0)) / 1048576), Path(data["pdf"])


def clean_images(root: Path, jm_id: str) -> str:
    """PDF 已成功发出去后再删原图，避免发送失败时连底稿都没了。"""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "merge_pdf.py"),
        "--id", jm_id,
        "--root", str(root),
        "--clean-only",
    ]
    code, output = run_cmd(cmd, 300)
    try:
        data = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "清理失败：%s" % output[-200:]
    if data.get("error"):
        return "清理失败：%s" % data["error"]
    return "已删原图 %s 张" % data.get("removed", 0)


def status_text(state: dict, root: Path) -> str:
    busy = state.get("busy")
    lines = ["状态：" + (("正在下载 %s" % busy) if busy else "空闲")]
    try:
        usage = shutil.disk_usage(str(root))
        lines.append("库目录：%s（剩余 %.1f GB）" % (root, usage.free / 1024 ** 3))
    except OSError:
        lines.append("库目录：%s（读不到）" % root)
    last = state.get("lastJm")
    if last:
        lines.append("上次任务：%s %s" % (last.get("id"), last.get("result")))
    return "\n".join(lines)


def handle_text(text: str, state: dict, root: Path) -> tuple[str, str | None]:
    """返回 (立即回复, jm_id 或 None)。"""
    lowered = text.strip().lower()
    if lowered in ("help", "?", "？", "菜单"):
        return HELP_TEXT, None
    if lowered in ("status", "状态"):
        return status_text(state, root), None
    match = JM_RE.match(text.strip()) or DOWNLOAD_RE.match(text.strip())
    if match:
        return "", match.group(1)
    return "我只认 jm <id> / status / help。发 help 看说明。", None


# ---------------------------------------------------------------------- 主循环


def process_message(msg: dict, account: dict, state: dict, root: Path, allow: set[str]) -> None:
    key = message_key(msg)
    if key in state.get("seen", []):
        return
    state.setdefault("seen", []).append(key)

    if not is_allowed(msg, allow):
        log("忽略非白名单消息 from=%s" % (msg.get("from_user_id") or "?"))
        return

    created = msg.get("create_time_ms") or 0
    if created and (time.time() * 1000 - created) > SKIP_OLDER_THAN_MS:
        log("忽略过期消息 %s（超过 1 小时）" % key)
        return

    text = extract_text(msg)
    peer = msg.get("from_user_id")
    ctx = msg.get("context_token")
    if not text:
        reply(account, peer, ctx, "只认文字命令，发 help 看说明。")
        return

    log("收到 %s: %r" % (peer, text[:60]))
    immediate, jm_id = handle_text(text, state, root)
    if immediate:
        reply(account, peer, ctx, immediate)
        return

    assert jm_id
    reply(account, peer, ctx, "收到，开始下载 JM %s，完成后把 PDF 发你。" % jm_id)
    state["busy"] = jm_id
    persist_account(account)
    save_state(state)

    def _progress(text: str) -> None:
        reply(account, peer, ctx, text)

    ok, detail, pdf = download_jm(jm_id, root, progress=_progress)
    state["busy"] = None
    state["lastJm"] = {"id": jm_id, "result": "ok" if ok else detail, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_state(state)

    sent = False
    if ok and pdf is not None:
        reply(account, peer, ctx, "✅ JM %s %s" % (jm_id, detail), media=pdf)
        sent = True
    else:
        reply(account, peer, ctx, "❌ JM %s 失败：%s" % (jm_id, detail))
    log("任务结束 %s ok=%s %s" % (jm_id, ok, detail))
    # 文件确实发出去了才清原图；发失败就留着，方便重试
    if sent:
        log("清理原图 %s: %s" % (jm_id, clean_images(root, jm_id)))


def serve(account: dict, state: dict, root: Path, allow: set[str], once: bool = False) -> int:
    if not once:
        lock = acquire_lock()
        if lock is None:
            log("已有实例在运行（bot.lock 被占用），本次退出")
            return 0
    rotate_log()
    log("启动：account=%s root=%s 白名单=%s" % (account["accountId"], root, ",".join(sorted(allow))))
    # 后台发「已就绪」，不挡长轮询：用户收到这条就说明整条链路通了
    threading.Thread(target=notify_ready, args=(account, allow), daemon=True).start()
    idle_rounds = 0
    while True:
        msgs = wx.fetch_updates(account, timeout=35)
        persist_account(account)
        if not msgs:
            idle_rounds += 1
            if once:
                log("没有新消息，退出（--once）")
                return 0
            continue
        idle_rounds = 0
        for msg in msgs:
            try:
                process_message(msg, account, state, root, allow)
            except Exception as err:  # noqa: BLE001 - 单条失败不影响循环
                log("处理消息出错: %r" % (err,))
        if once:
            log("处理完 %d 条，退出（--once）" % len(msgs))
            return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    if sys.stdout is None or sys.stderr is None:
        sink = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = sink
        if sys.stderr is None:
            sys.stderr = sink

    parser = argparse.ArgumentParser(description="微信命令桥（jm / help / status，无模型）")
    parser.add_argument("--serve", action="store_true", help="常驻长轮询")
    parser.add_argument("--once", action="store_true", help="处理当前积压后退出")
    parser.add_argument("--selftest", action="store_true", help="不联网，自检命令解析")
    parser.add_argument("--account", help="账号 id 或前缀")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="漫画库根目录")
    parser.add_argument("--allow", action="append", default=[], help="额外允许的 openid，可重复")
    args = parser.parse_args(argv)

    root = Path(args.root)
    state = load_state()

    if args.selftest:
        cases = ["jm1088227", "jm 123", "下载 555", "help", "status", "你好"]
        for case in cases:
            immediate, jm_id = handle_text(case, state, root)
            print(json.dumps({"input": case, "jm": jm_id, "reply": immediate[:40]}, ensure_ascii=False))
        print(json.dumps({"state": str(STATE_PATH), "log": str(LOG_PATH), "root": str(root)}, ensure_ascii=False))
        return 0

    account = wx.pick_account(wx.load_accounts(), args.account)
    allow = {account.get("userId")} | {p for p in (account.get("contextTokens") or {})} | set(args.allow)
    allow.discard(None)

    if not (args.serve or args.once):
        parser.error("用 --serve 常驻，或 --once 处理一次")

    try:
        return serve(account, state, root, allow, once=args.once)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
        return 0


if __name__ == "__main__":
    sys.exit(main())
