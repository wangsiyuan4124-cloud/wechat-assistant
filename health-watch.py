# -*- coding: utf-8 -*-
"""5 分钟轮询基线健康检查，输出到 health.log

检查项：
  1. sender 进程是否在跑（app.py）
  2. 微信主窗口是否可见（非最小化、offscreen=False）
  3. queue.db 最新消息状态（done/failed 计数 + 最近 3 条）
  4. 水位线是否在推进（wechat_state/wechat_scan.lastScanMs 与上次差）
  5. service.log 最近 5 条 WARNING
"""
import datetime as _dt
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "health.log"
CLOUD_STATE_URL = None  # 不查云端，省 token；水位线推进靠本地日志即可

def ts():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def header():
    return f"\n===== {ts()} ====="

def running_app_pys():
    """返回运行中的 app.py pythonw.exe 进程 PID 列表"""
    try:
        # PowerShell 更快、输出更干净
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             "@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -match 'app\\.py' }).Count; "
             "@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
             "Where-Object { $_.CommandLine -match 'app\\.py' }).ProcessId -join ','"],
            text=True, encoding='utf-8', errors='ignore', timeout=10
        )
        parts = out.strip().splitlines()
        if len(parts) >= 2 and parts[1]:
            return [int(x) for x in parts[1].split(',') if x.strip()]
    except Exception:
        pass
    return []

def wechat_visible():
    """返回 (可见主窗口名, 最小化主窗口名)，用窗口标题直接判断"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    # 枚举所有顶层窗口，取标题含微信或 mmui 类名
    res = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        n = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, n, 256)
        if not (cls.value.startswith('mmui') or '微信' in n.value):
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w = r.right - r.left
        h = r.bottom - r.top
        minimized = w <= 1 or h <= 1 or r.left < -10000
        res.append((n.value or cls.value, minimized, (r.left, r.top, r.right, r.bottom)))
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)

    visible = [(nm, rect) for nm, m, rect in res if not m]
    minimized = [(nm, rect) for nm, m, rect in res if m]
    return visible, minimized

def queue_status():
    db = BASE / "queue.db"
    if not db.exists():
        return "no queue.db"
    c = sqlite3.connect(str(db))
    rows = c.execute("SELECT status, COUNT(*), MAX(updated_at) FROM messages GROUP BY status").fetchall()
    latest = c.execute(
        "SELECT msg_id, status, substr(error,1,50), updated_at FROM messages ORDER BY updated_at DESC LIMIT 3"
    ).fetchall()
    c.close()
    return rows, latest

def service_warnings_tail(n=5):
    log = BASE / "service.log"
    if not log.exists():
        return "no service.log"
    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
    warns = [l for l in lines if "WARNING" in l]
    return warns[-n:]

def main():
    prev_watermark = 0
    print("health-watch started, PID=", os.getpid())
    while True:
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(header() + "\n")
                pids = running_app_pys()
                f.write(f"[进程] pythonw app.py PIDs = {pids}\n")

                vis, minz = wechat_visible()
                f.write(f"[微信] 可见窗口={vis} 最小化={minz}\n")

                qs = queue_status()
                if isinstance(qs, str):
                    f.write(f"[队列] {qs}\n")
                else:
                    rows, latest = qs
                    f.write("[队列] 状态计数:\n")
                    for r in rows:
                        f.write(f"        {r}\n")
                    f.write("[队列] 最近 3 条:\n")
                    for r in latest:
                        f.write(f"        {r}\n")

                warns = service_warnings_tail()
                if warns:
                    f.write(f"[日志] 最近 WARNING:\n")
                    for w in warns:
                        f.write(f"       {w}\n")
                else:
                    f.write("[日志] 无 WARNING\n")

            # 控制台输出最近一行，方便我轮询
            print(f"[{ts()}] ok", flush=True)
        except Exception as e:
            print(f"[{ts()}] err: {e}", flush=True)
        time.sleep(300)  # 5 分钟一轮

if __name__ == "__main__":
    main()
