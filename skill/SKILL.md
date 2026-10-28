---
name: jm-weixin-bot
description: 运维「微信 JM 命令桥」：在微信里发 jm <id> 自动下载 JM 漫画、合并 PDF 并回传。用于启动/停止/查看状态、扫码登录微信、排查不回消息或下载失败、以及手动把 PDF 推到微信。用户提到微信 bot、jm <id> 没反应、要重启/查看这个 bot 时使用。
---

# 微信 JM 命令桥 · 运维

包目录（按实际安装位置替换 `$BOT`）：`$BOT` = 本 skill 的上一级目录，里面应有
`start-bot.sh`、`skill/scripts/`、`autostart/`。

## 启停与状态

```bash
bash "$BOT/start-bot.sh"            # 启动（幂等，已在跑会跳过）
bash "$BOT/start-bot.sh" status     # pid、库剩余空间、最近 5 行日志
bash "$BOT/start-bot.sh" restart    # 改过脚本后必须重启
bash "$BOT/start-bot.sh" stop
```

日志：`~/.jm-weixin/bot.log`（stdout）和 `~/.dsh/weixin/bot.log`（脚本内部写）。
时间戳是北京时间（`TZ=UTC-8`）。

## 扫码登录 / 换账号

```bash
cd "$BOT/skill/scripts"
python3 send_weixin.py --login-server --port 18791
# 浏览器打开 http://127.0.0.1:18791/
```

登录成功会把账号写到 `~/.dsh/weixin/accounts/*.json`。同一个 bot 只能一台常驻。

## 手动发消息 / 发文件

```bash
python3 "$BOT/skill/scripts/send_weixin.py" --check
python3 "$BOT/skill/scripts/send_weixin.py" -m "测试" --json
python3 "$BOT/skill/scripts/send_weixin.py" -m "JM 123 已下载" --media "/path/JM_123.pdf" --json
```

`-2 prepare failed` = 微信会话窗口过期：先在微信里给 bot 发一句话，再加 `--sync` 重试。

## 排错顺序

1. `start-bot.sh status`：没在跑就先 `start`；
2. 启动日志里有没有「已发送就绪通知」：有 = 链路通；
3. 微信不回：看日志有没有「收到 …」；有收到但没回 = 发送被拒（多半 -2）；
4. 「不存在或不可见」= ID 错或该本需登录；
5. 下载慢看进度消息页数是否在涨；不涨就是图床/CDN 问题。

## 自动启动

```bash
bash "$BOT/autostart/enable-autostart.sh"
bash "$BOT/autostart/disable-autostart.sh"
```

只适用于 DSHA/DSH 容器；普通 Linux 用 systemd（见 README）。

## 注意

- 微信要先能看到 **clawbot 插件**（我 → 设置 → 插件）：微信升级到最新版 → 杀掉微信进程 → 重开；看不到就再杀一次重开。没有这个插件时扫码后可能找不到 bot 或发消息没反应；
- 凭证明文存 `~/.dsh/weixin/accounts/`，不要外传；
- 非官方接口，有风控风险；
- 库目录 `~/JM_Downloads`，成品 `<id>/JM_<id>.pdf`，发出去后自动删原图。
