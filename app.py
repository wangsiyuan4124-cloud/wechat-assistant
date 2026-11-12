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

import subprocess

import store
from wechat_client import make_client, com_init

BASE = Path(__file__).resolve().parent

# ---------- 单实例锁 ----------
# Windows 上 Python 的 fcntl 不可用，用 msvcrt.locking 或直接 CreateMutex
try:
    import msvcrt
    _lock_file = open(BASE / ".app.pid.lock", "w")
    try:
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
    # 锁不可用时不阻断，毕竟偶发冲突对只发不收影响有限
    pass
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
AUTO_RESTART = bool(CFG.get("watch", {}).get("auto_restart_wechat", True))
WECHAT_EXE = str(CFG.get("watch", {}).get("wechat_exe", ""))
STUCK_THRESHOLD = int(CFG.get("watch", {}).get("stuck_threshold_sec", 120))
ALERT_COOLDOWN = 1800  # 掉线告警冷却 30 分钟

wx = make_client(CFG["wechat"].get("window_names", ["微信", "WeChat"]), log=log.info)
_send_lock = threading.Lock()


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
        return d.get("items", [])
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

def _restore_wechat_window():
    """把微信主窗口从各种"不正常"状态拉回到可见可操作。

    覆盖场景：
    1. 最小化到任务栏（rect 超出屏幕或尺寸为 0）
    2. **Qt6 丢失 WS_VISIBLE 样式位**（窗口看着在屏幕上但 IsWindowVisible=False）
       — 微信 4.x 切到 Qt6 后频繁触发此问题，WeChatGUI 只枚举 IsWindowVisible 的窗口
       — 用 ShowWindow(hwnd, SW_SHOW) 强制把可见样式位加回去
    """
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    # 枚举所有顶层窗口，找微信主窗（Qt 类名前缀 or 标题含微信）
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found_hwnd = None
    found_name = ""
    def cb(hwnd, _):
        nonlocal found_hwnd, found_name
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        n = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, n, 256)
        if cls.value.startswith("Qt") and "微信" in n.value:
            found_hwnd = hwnd
            found_name = n.value
            return False  # 停止枚举
        return True
    user32.EnumWindows(CB(cb), 0)

    if not found_hwnd:
        return False

    needs_fix = False
    reason = []

    # 检查 1：IsWindowVisible
    if not user32.IsWindowVisible(found_hwnd):
        needs_fix = True
        reason.append("IsWindowVisible=False（Qt6 丢失 WS_VISIBLE 样式位）")

    # 检查 2：最小化特征（rect 尺寸 0 或坐标在 -32000 附近）
    rr = wintypes.RECT()
    user32.GetWindowRect(found_hwnd, ctypes.byref(rr))
    w = rr.right - rr.left
    h = rr.bottom - rr.top
    if w <= 1 or h <= 1 or rr.left < -10000:
        needs_fix = True
        reason.append(f"窗口尺寸/位置异常 rect=({rr.left},{rr.top},{rr.right},{rr.bottom})")

    if needs_fix:
        # 强制 ShowWindow — 恢复可见性（同时处理 Qt6 样式位丢失和最小化）
        user32.ShowWindow(found_hwnd, 5)   # SW_SHOW
        user32.ShowWindow(found_hwnd, 9)   # SW_RESTORE
        time.sleep(0.2)
        # 放回屏幕中央可见区
        user32.SetWindowPos(found_hwnd, -1, 120, 40, 700, 560, 0x0040)
        log.info(f"已修复微信窗口 ({found_hwnd}): {'; '.join(reason)}")
    return True


def poll_loop():
    """定时从云端拉取待发消息入本地队列"""
    while True:
        for it in fetch_pending():
            group, text, msg_id = it.get("group", ""), it.get("text", ""), it.get("msgId", "")
            if group not in ALLOWED_GROUPS:
                log.warning(f"群「{group}」不在白名单，回执失败: {msg_id}")
                ack(msg_id, False, f"群「{group}」不在白名单")
                continue
            if store.enqueue(msg_id, group, text):
                log.info(f"入队 {msg_id} → {group}（{len(text)} 字）")
            else:
                # 云端超时重放（如上次回执前进程被杀）：按本地现状补回执，避免云端反复重投
                st = store.get_status(msg_id)
                if st == "done":
                    log.info(f"重复消息 {msg_id} 本地已完成，补回执 done")
                    ack(msg_id, True)
                elif st == "failed":
                    log.info(f"重复消息 {msg_id} 本地已失败，补回执 failed")
                    ack(msg_id, False, "本地发送失败(历史记录)")
                # pending/sending 状态由 worker 完成后正常回执
        time.sleep(POLL)


def worker_loop():
    """串行消费队列：微信 UI 操作必须独占，避免并发冲突。

    加固点：
    - 微信离线类错误直接跳过重试（重启微信后才能发）
    - 普通错误走 3 次退避重试
    - 硬超时由 watcher 线程的 stuck reset 兜底（sending 超过 120s 强制重置）
    """
    com_init()
    RETRY_SLEEPS = (10, 60, 180, 300)  # 4 次退避重试间隔（秒）
    while True:
        row = store.claim_next()
        if not row:
            time.sleep(2)
            continue
        msg_id = row["msg_id"]
        grp = row["grp"]
        text = row["text"]
        last_err = None
        ok = False
        hard_fail = False
        for attempt, slp in enumerate((0,) + RETRY_SLEEPS):
            if attempt > 0:
                log.warning(f"本地重试 #{attempt}，等 {slp}s 后再发 {msg_id}")
                time.sleep(slp)
            try:
                _restore_wechat_window()
                with _send_lock:
                    wx.send_to_group(grp, text)
                ok = True
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
                log.warning(f"发送失败 attempt={attempt+1} {msg_id}: {last_err[:120]}")
                # 微信离线/UIA 不可用 类错误：直接跳过重试（重启微信后才能发）
                if any(kw in last_err for kw in (
                    "未找到微信", "疑似离线", "登录状态检测失败",
                    "UIA 引擎不可用", "找不到 ChatRoom",
                )):
                    log.error(f"微信不可用或群聊搜索失败，停止重试 {msg_id}")
                    hard_fail = True
                    break
        try:
            if ok:
                store.finish(msg_id, True)
                log.info(f"完成 {msg_id}")
                ack(msg_id, True)
            else:
                store.finish(msg_id, False, last_err or "unknown")
                log.error(f"最终失败 {msg_id} (hard_fail={hard_fail}): {last_err}")
                ack(msg_id, False, last_err or "unknown")
                _alert(f"微信发送最终失败：{grp}（{msg_id}）\n{last_err or 'unknown'}")
        except Exception as e:
            log.error(f"回执/落库异常 {msg_id}: {e}")
        time.sleep(2)


def watcher_loop():
    """定时检测微信登录状态 + 自动还原窗口 + 进程守护 + 卡死重置。

    加固点：
    1. 微信离线 → 自动拉起 Weixin.exe（需 config.watch.wechat_exe 配置）
    2. sending 状态超过 STUCK_THRESHOLD 秒 → 强制重置为 pending（worker 硬超时兜底之外的保险）
    3. 窗口最小化 → 自动还原（已有逻辑）
    4. 离线告警冷却 30 分钟（避免刷屏）
    """
    com_init()
    last_alert = 0.0
    last_restart = 0.0  # 进程重启冷却，避免启动风暴
    while True:
        try:
            # ---- 1. 进程守护：微信没了就拉起 ----
            if AUTO_RESTART and WECHAT_EXE:
                if not wx.is_logged_in():
                    now = time.time()
                    if now - last_restart > 300:  # 5 分钟冷却一次
                        log.warning(f"微信进程不在，尝试拉起: {WECHAT_EXE}")
                        try:
                            subprocess.Popen(
                                [WECHAT_EXE],
                                creationflags=subprocess.CREATE_NO_WINDOW,
                            )
                            last_restart = now
                            log.info("微信拉起成功，等待用户扫码登录")
                        except Exception as e:
                            log.error(f"微信拉起失败: {e}")
                    else:
                        log.info("微信不在重启冷却期内，跳过自动拉起")

            # ---- 2. 离线告警 ----
            if not wx.is_logged_in():
                log.warning("微信疑似离线/未登录")
                if time.time() - last_alert > ALERT_COOLDOWN:
                    _alert("微信已离线或未登录，群消息发送服务暂停，请到本机扫码登录！")
                    last_alert = time.time()
            else:
                # 在线时顺便还原窗口
                _restore_wechat_window()

            # ---- 3. 卡死重置：sending 超过阈值秒 → 放回 pending ----
            import sqlite3
            db = BASE / "queue.db"
            if db.exists():
                c = sqlite3.connect(str(db))
                stuck = c.execute(
                    "SELECT msg_id FROM messages WHERE status='sending' "
                    "AND julianday('now','localtime') - julianday(updated_at) > ? / 86400.0",
                    (STUCK_THRESHOLD,),
                ).fetchall()
                if stuck:
                    ids = [r[0] for r in stuck]
                    log.warning(
                        f"发现 {len(ids)} 条 sending 超过 {STUCK_THRESHOLD}s，强制重置为 pending: {ids}")
                    c.executemany(
                        "UPDATE messages SET status='pending', error='watchdog_stuck_reset', "
                        "updated_at=datetime('now','localtime') WHERE msg_id=?",
                        [(i,) for i in ids],
                    )
                    c.commit()
                c.close()
        except Exception as e:
            log.error(f"watcher 异常: {e}")
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
    store.requeue_stuck()  # 重启续发
    # 服务运行期间阻止系统自动睡眠（锁屏仍会导致发送失败，需保持不锁屏）
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    log.info(f"微信发送服务启动（轮询模式）：{PULL_URL}，间隔 {POLL}s，白名单群 {len(ALLOWED_GROUPS)} 个")
    while True:
        time.sleep(3600)
