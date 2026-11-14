# -*- coding: utf-8 -*-
"""微信群消息发送服务（轮询模式）

链路：业务触发 → 云函数 push-wechat → 云数据库 wechat_outbox
      → 本服务定时轮询 pull-wechat 拉取 → wechatauto UIA 发群 → ack 回执云端

无需公网入口/内网穿透：本机只发出站 HTTPS 请求。
安全：云端 pull-wechat 校验 X-Token；本机再校验群白名单；msg_id 本地 SQLite 去重。
"""
import ctypes
import logging
import os
import threading
import time
from pathlib import Path

import requests
import yaml

from datetime import datetime
import sys

import store


BASE = Path(__file__).resolve().parent

# ---------- 单实例锁 ----------
# Windows 上 Python 的 fcntl 不可用，用 msvcrt.locking 或直接 CreateMutex
try:
    import msvcrt
    _lock_file = open(BASE / ".app.pid.lock", "a+")
    try:
        _lock_file.seek(0)
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        import sys
        print("另一个 app.py 已在运行，退出。", file=sys.stderr)
        _lock_file.close()
        sys.exit(1)
    _lock_file.seek(0)
    _lock_file.truncate()
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()
except Exception as _e:
    raise RuntimeError("无法取得单实例锁，停止启动") from _e
CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "service.log", encoding="utf-8")],
    force=True,  # 覆盖 wechatauto 的默认日志配置，确保写入 service.log
)
log = logging.getLogger("wechat-sender")

PULL_URL = CFG["cloud"]["pull_url"]
TOKEN = str(CFG["cloud"]["token"])
POLL = float(CFG["cloud"].get("poll_interval_sec", 15))
ALLOWED_GROUPS = set(CFG["wechat"]["groups"])
WATCH = float(CFG.get("watch", {}).get("interval_sec", 60))
ALERT_COOLDOWN = 1800  # 掉线告警冷却 30 分钟

if CFG['wechat'].get('engine', 'rpa') != 'rpa':
    raise RuntimeError('本加固版本只启用 RPA；Hook 分支未验证，不允许直接启用')


# ---------- 云端交互 ----------

def cloud_call(payload, timeout=10):
    r = requests.post(PULL_URL, json=payload, headers={"X-Token": TOKEN}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_pending(limit=10):
    try:
        d = cloud_call({"action": "fetch", "limit": limit})
        if not d.get("ok"):
            log.warning(f"fetch 被拒绝: {d.get('msg')}")
            return []
        items = d.get('items', [])
        if not isinstance(items, list):
            raise ValueError('items must be a list')
        return items
    except Exception as e:
        log.error(f"fetch 失败: {e}")
        return []


def ack(msg_id, ok, error=""):
    try:
        d = cloud_call({"action": "ack", "msgId": msg_id, "ok": ok, "error": error})
        if not d.get("ok"):
            log.error(f"ack 被拒绝 {msg_id}: {d.get('msg')}")
    except Exception as e:
        log.error(f"ack 失败 {msg_id}: {e}（本地状态已记录，云端将超时重放）")


# ---------- 后台线程 ----------

def poll_loop():
    """定时从云端拉取待发消息入本地队列"""
    while True:
        for it in fetch_pending():
            if not isinstance(it, dict) or not all(isinstance(it.get(k), str) and it[k].strip() for k in ('group','text','msgId')):
                log.error('云端任务结构无效，跳过')
                continue
            group, text, msg_id = it.get("group", ""), it.get("text", ""), it.get("msgId", "")
            if group not in ALLOWED_GROUPS:
                log.warning(f"群「{group}」不在白名单，回执失败: {msg_id}")
                ack(msg_id, False, f"群「{group}」不在白名单")
                continue
            try:
                raw_exp = it.get('expiresAt')
                if raw_exp:
                    dt = datetime.fromisoformat(str(raw_exp).replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        raise ValueError('expiresAt 必须包含时区')
                    expiry = dt.timestamp()
                else:
                    expiry = time.time() + int(CFG.get('delivery', {}).get('default_ttl_sec', 3600))
                added = store.enqueue(msg_id, group, text, expiry)
            except (ValueError, TypeError, OverflowError) as e:
                log.error('无效任务 %r: %s', msg_id, e)
                continue
            if added:
                log.info(f"入队 {msg_id} → {group}（{len(text)} 字）")
            else:
                # 云端超时重放（如上次回执前进程被杀）：按本地现状补回执，避免云端反复重投
                st = store.get_status(msg_id)
                if st == "done":
                    log.info(f"重复消息 {msg_id} 本地已完成，补回执 done")
                    ack(msg_id, True)
                elif st in ("failed", "expired"):
                    log.info(f"重复消息 {msg_id} 本地已失败，补回执 failed")
                    ack(msg_id, False, "本地发送失败(历史记录)")
                elif st == 'uncertain':
                    log.warning('任务 %s 等待人工核查；不回执失败、不重发', msg_id)
                # pending/sending 状态由 worker 完成后正常回执
        time.sleep(POLL)


def worker_loop():
    from delivery import deliver
    while True:
        row = store.claim_next()
        if row is None:
            time.sleep(2)
            continue
        try:
            status, error = deliver(dict(row), CFG, BASE)
        except Exception as e:
            status, error = 'uncertain', f'delivery boundary error: {e}'
        store.set_status(row['msg_id'], status, error)
        if status == 'pending':
            time.sleep(15)
            continue
        if status in ('done', 'failed', 'expired'):
            ack(row['msg_id'], status == 'done', error)
        if status == 'uncertain':
            _alert(f"发送结果待核查，禁止自动重试：{row['msg_id']} / {error}")
        time.sleep(2)


def watcher_loop():
    # Read-only health summary. Never manipulate or start WeChat here.
    while True:
        log.info("队列状态: %s", store.queue_depth())
        time.sleep(WATCH)


def _alert(text):
    url = (CFG.get("alerts") or {}).get("wechat_work_webhook") or ""
    if not url:
        log.warning(f"[备用告警未配置，仅记录] {text}")
        return
    try:
        requests.post(url, json={"msgtype": "text", "text": {"content": f"[微信发送服务] {text}"}}, timeout=5)
    except Exception as e:
        log.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    store.init_db()
    store.requeue_stuck()  # 重启后保留待核查状态，禁止自动续发
    # 服务运行期间阻止系统自动睡眠（锁屏仍会导致发送失败，需保持不锁屏）
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    log.info(f"微信发送服务启动（轮询模式）：{PULL_URL}，间隔 {POLL}s，白名单群 {len(ALLOWED_GROUPS)} 个")
    while True:
        time.sleep(3600)
