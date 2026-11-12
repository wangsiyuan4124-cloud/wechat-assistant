# -*- coding: utf-8 -*-
"""微信发送客户端：双引擎

- wcferry（hook，主力）：注入微信 3.9.12.51，API 级发送。
  按 wxid 精确投递，天然零错群；最小化/锁屏均可发送。
- wechatauto（RPA，备用）：UIA 控件树操作微信 4.x。
  已加固：精确匹配 ChatRoom section + 严格名称校验 + 单条超时保护。

config.yaml → wechat.engine: "wcferry" | "rpa"
"""
import threading
import time

import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _engine_name():
    try:
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
        return (cfg.get("wechat") or {}).get("engine", "wcferry")
    except Exception:
        return "wcferry"


class WcferryClient:
    """hook 引擎：API 级收发，无 UI 依赖"""

    def __init__(self, window_names=None, log=print):
        self.log = log
        self._wcf = None
        self._lock = threading.Lock()

    def _get(self):
        if self._wcf is None:
            from wcferry import Wcf
            self._wcf = Wcf(debug=False)
            self.log("[wcferry] 已注入微信，RPC 连接成功")
        return self._wcf

    def is_logged_in(self):
        try:
            with self._lock:
                return bool(self._get().is_login())
        except Exception as e:
            self.log(f"[wcferry] 登录状态检测失败: {e}")
            return False

    def _room_id(self, group):
        w = self._get()
        for c in w.get_contacts():
            if not str(getattr(c, "wxid", "")).endswith("@chatroom"):
                continue
            if group in (getattr(c, "name", ""), getattr(c, "remark", "")):
                return c.wxid
        return None

    def send_to_group(self, group, text, retries=3):
        last_err = None
        for i in range(1, retries + 1):
            try:
                with self._lock:
                    w = self._get()
                    rid = self._room_id(group)
                    if not rid:
                        raise RuntimeError(
                            f"联系人列表中找不到群「{group}」"
                            f"（请确认本微信账号已在群内）")
                    if not w.send_text(text, rid):
                        raise RuntimeError("send_text 返回失败")
                self.log(f"[wcferry] 已发送 → {group}（{rid}）")
                return
            except Exception as e:
                last_err = str(e)
                self.log(f"[wcferry] 第 {i}/{retries} 次发送失败: {last_err}")
                time.sleep(2)
        raise RuntimeError(f"发送到「{group}」重试 {retries} 次均失败: {last_err}")


# =========================================================================
# RPA 引擎：加固版
# =========================================================================

class RpaClient:
    """RPA 备用引擎：wechatauto UIA 操作微信 4.x

    加固点：
    1. open_chat 强制加 section='ChatRoom' 过滤——只在群聊里搜，绝不碰个人会话/系统账号
    2. open_chat 返回后强校验 exact name——搜索可能返回近似结果，必须轮询到完全匹配
    3. send_text 重试从 2 次升到 3 次
    4. **不靠 ThreadPoolExecutor 做硬超时**——UIA COM 是线程亲和的（STA 模型），
       必须在 InitializeUIAutomationInCurrentThread 的同一个线程里使用。
       硬超时由每个步骤内部的 timeout 参数 + watcher 的 stuck reset 兜底。
    """

    def __init__(self, window_names=None, log=print):
        self.window_names = list(window_names or ("微信", "WeChat"))
        self.log = log
        self._gui = None
        self._uia = None

    def _engine(self):
        """惰性创建 UIA 引擎；不可用时抛错（绝不降级 OCR）。"""
        if self._gui is None:
            from wechatauto.guia import WeChatGUI
            self._gui = WeChatGUI()
        if self._uia is None:
            self._uia = self._gui._get_uia()
        if self._uia is None:
            raise RuntimeError("UIA 引擎不可用（无障碍门热激活失败），按安全策略中止发送")
        return self._gui, self._uia

    def is_logged_in(self):
        """微信进程存活 + 窗口可见性 OK 视为在线。"""
        import ctypes
        import subprocess
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # 进程活
        def has(image):
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return image in (r.stdout or b"").decode("utf-8", "ignore")

        if not (has("Weixin.exe") or has("WeChat.exe")):
            return False

        # 窗口可见——兼容 Qt6 丢失 WS_VISIBLE 样式位的场景
        hits = []
        CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            n = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, n, 256)
            if cls.value.startswith("Qt") or "微信" in n.value:
                hits.append(hwnd)
            return True
        user32.EnumWindows(CB(cb), 0)
        return len(hits) > 0

    # ---------- 加固版 open_chat ----------

    def _ensure_chat(self, uia, group, timeout=10):
        """确保当前会话是目标群。加固版：强制 ChatRoom section + 严格名称校验。"""
        cur = uia.current_chat()
        if cur == group:
            return True
        self.log(f"[wechat] 当前会话={cur!r}，目标={group!r}，开始搜索")

        # 强制带 section='ChatRoom'——只在群聊结果里搜，绝不碰个人/系统账号
        try:
            ok = uia.open_chat(group, section="ChatRoom")
        except TypeError:
            ok = uia.open_chat(group)

        if not ok:
            self.log(f"[wechat] 搜索返回 False，找不到 ChatRoom 匹配的「{group}」")
            return False

        # 严格轮询校验 exact name——防止搜索点到相似名
        deadline = time.time() + timeout
        last_seen = None
        while time.time() < deadline:
            last_seen = uia.current_chat()
            if last_seen == group:
                return True
            time.sleep(0.4)

        self.log(f"[wechat] open_chat 后校验失败：最后看到={last_seen!r}，期望={group!r}")
        return False

    # ---------- send_to_group ----------

    def send_to_group(self, group, text, retries=3):
        """发送文本到指定群。加固：ChatRoom section + exact name 强校验。

        注意：UIA COM 必须在同一个线程里 Initialize + 使用，
        所以这里**不做线程隔离**。硬超时由调用方的 watcher stuck reset 兜底。
        """
        gui, uia = self._engine()
        from wechatauto.param import WxParam
        WxParam.ENABLE_OCR = False  # 安全红线：禁用一切坐标盲猜兜底
        last_err = None
        for i in range(1, retries + 1):
            try:
                mark = None
                try:
                    mark = gui._send_watermark(group)
                except Exception as e:
                    self.log(f"[wechat] 水位获取失败（跳过 DB 回执）: {e}")

                if not self._ensure_chat(uia, group):
                    cur = uia.current_chat()
                    raise RuntimeError(
                        f"未能确认当前会话为「{group}」（当前={cur!r}），已中止发送")

                if not uia.send_text(text):
                    raise RuntimeError("send_text 未确认（回读不达标，已拒绝回车）")

                if mark is not None:
                    try:
                        if gui._wait_landed(text, group, mark):
                            self.log(f"[wechat] 已发送并确认 → {group}")
                        else:
                            self.log(f"[wechat] 已发送 → {group}（DB 回执未确认，UIA 校验已通过）")
                    except Exception as e:
                        self.log(f"[wechat] 已发送 → {group}（DB 回执异常: {e}）")
                else:
                    self.log(f"[wechat] 已发送 → {group}（无水位，跳过 DB 回执）")
                return
            except Exception as e:
                last_err = str(e)
                self.log(f"[wechat] 第 {i}/{retries} 次发送失败: {last_err}")
                time.sleep(2)
        raise RuntimeError(f"发送到「{group}」重试 {retries} 次均失败: {last_err}")


def com_init():
    if _engine_name() == "wcferry":
        return
    try:
        import uiautomation as auto
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass


def make_client(window_names=None, log=print):
    engine = _engine_name()
    log(f"[engine] 使用 {engine} 引擎")
    if engine == "wcferry":
        return WcferryClient(log=log)
    return RpaClient(window_names=window_names, log=log)
