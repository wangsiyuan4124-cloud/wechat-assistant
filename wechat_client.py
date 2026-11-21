# -*- coding: utf-8 -*-
"""微信发送引擎：RPA（wechatauto）/ agent-desktop CLI / Wcferry。

选哪个由 config.yaml wechat.engine 决定。
- agent-desktop：Vercel Labs 开源 CLI（npm install -g agent-desktop），
  原生 Rust + Windows UI Automation，读 UI 树不关心窗口离屏/最小化。
- wcferry：微信 3.9.12.x hook 注入（API 级，零错群，不依赖窗口）。
- rpa：wechatauto UIA 操作微信 4.x（历史方案，窗口被最小化就崩）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent


def _engine_name():
    try:
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
        return (cfg.get("wechat") or {}).get("engine", "agent-desktop")
    except Exception:
        return "agent-desktop"


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _run(cmd, timeout=15, log=None):
    """subprocess 执行命令，捕获 stdout/stderr 返回文本。"""
    if log:
        log(f"[cmd] {' '.join(cmd)}")
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            raise RuntimeError(f"exit={p.returncode}: {out.strip()[:200]}")
        return out.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时 {timeout}s: {' '.join(cmd)}")


def _cli_path(name):
    """优先找原生 exe，再回退 PATH 里的 shim。"""
    p = Path(shutil.which(name) or "")
    if p.suffix.lower() in (".exe", ""):
        # npm -g 的 shim 是无扩展名，Windows 下 subprocess 不能直接跑
        if p.suffix == "" and p.name == name:
            candidate = p.parent / f"{name}.cmd"
            if candidate.exists():
                return str(candidate)
        return str(p) if p.exists() else None
    # 优先 npm 全局原生二进制路径
    for suffix in ("-win32-x64.exe", "-win32-arm64.exe", ".exe"):
        npm_root = Path(os.environ.get("APPDATA", "")) / "npm"
        candidate = npm_root / "node_modules" / name / "bin" / f"{name}{suffix}"
        if candidate.exists():
            return str(candidate)
    return str(p) if p.exists() else None


def _json_of(text):
    """从 agent-desktop 输出里提取最后一个 JSON 对象（前面可能有日志行）。"""
    if not text:
        return {}
    # 尝试直接 parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # 从后往前找第一个 { ... } 块
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# AgentDesktopClient —— Vercel Labs agent-desktop CLI
# ---------------------------------------------------------------------------

class AgentDesktopClient:
    """基于 agent-desktop CLI 的微信发送引擎（推荐）。

    原理：
    - agent-desktop 是 Vercel Labs 开源的 Windows/macOS 桌面自动化 CLI（Rust 写的）
    - 通过 Windows UI Automation 读 UI 树，**不依赖窗口可见性**
    - agent-desktop CLI 安装：npm install -g agent-desktop --allow-scripts=agent-desktop
      （npm 的 allow-scripts 在 npm>=10 上才能放行 postinstall 编译原生二进制）
    """

    APP = "Weixin"  # 进程名，agent-desktop 用这个定位应用

    def __init__(self, window_names=None, log=print):
        self.log = log
        bin_path = _cli_path("agent-desktop")
        if not bin_path:
            raise RuntimeError(
                "找不到 agent-desktop CLI，请先安装："
                "npm install -g agent-desktop --allow-scripts=agent-desktop"
            )
        self._cli = bin_path
        self.log(f"[agent-desktop] 二进制: {self._cli}")

    # ---------- 底层命令 ----------

    def _run(self, args, timeout=15, require_ok=True):
        cmd = [self._cli, *args]
        out = _run(cmd, timeout=timeout, log=self.log)
        data = _json_of(out)
        if require_ok and data.get("ok") is False:
            err = data.get("error") or out[:200]
            raise RuntimeError(f"agent-desktop 命令失败: {err}")
        return data

    def _snapshot(self):
        return self._run(["snapshot", "--app", self.APP, "-i"], timeout=20)

    def _find(self, role=None, name=None, text=None, exact=False, native_id=None):
        """在 UI 树里查找元素，返回 ref 字符串（如 '@s1aq3c386zbesu:e1'）。"""
        args = ["find", "--app", self.APP]
        if role:
            args += ["--role", role]
        if name:
            args += ["--name", name]
        if text:
            args += ["--text", text]
        if exact:
            args += ["--exact"]
        if native_id:
            args += ["--native-id", native_id]
        data = self._run(args, timeout=10, require_ok=False)
        if not data.get("ok"):
            return None
        inner = data.get("data") or {}
        matches = inner.get("matches") or []
        if not matches:
            return None
        return matches[0].get("ref_id")

    def _click(self, ref):
        self._run(["click", ref, "--headed"], timeout=10)

    def _type(self, ref, text):
        """往输入框填文本。

        agent-desktop 的 type 命令会把 \\n 当普通字符吞掉，导致多行消息挤成一团。
        所以改用 Windows 剪贴板 + ctrl+v 粘贴，和 RPA 引擎效果一致，能保留换行和 emoji。
        """
        self._paste_clipboard(text)
        # 用 click 聚焦输入框（但 click 后可能焦点丢失，所以用 press 先点再 v）
        self._run(["click", ref, "--headed"], timeout=10)
        import time
        time.sleep(0.15)  # 等焦点到位
        self._press("ctrl+v")

    @staticmethod
    def _paste_clipboard(text):
        """把文本写入 Windows 剪贴板（UTF-16），支持换行和 emoji。"""
        import subprocess
        # PowerShell Set-Clipboard 原生支持多行 + emoji，比 ctypes 稳
        # 用 -Raw 读 stdin，避免命令行转义吞换行
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$input | Set-Clipboard"],
                input=text, capture_output=True, timeout=5, encoding="utf-8"
            )
            return
        except Exception:
            pass  # PowerShell 失败就退回 ctypes

        # 兜底：ctypes 直接调 Win32 API（简单版，不加 GMEM_ZEROINIT 避免 int 溢出）
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.restype = ctypes.c_void_p

        data = (text + "\0").encode("utf-16-le")
        size = len(data)
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.c_size_t(size))
        if not h_mem:
            raise RuntimeError(f"GlobalAlloc 失败 (size={size})")
        buf = kernel32.GlobalLock(h_mem)
        ctypes.memmove(buf, data, size)
        kernel32.GlobalUnlock(h_mem)

        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(h_mem)
            raise RuntimeError("OpenClipboard 失败")
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)  # 剪贴板接管内存
        user32.CloseClipboard()

    def _clear(self, ref):
        self._run(["clear", ref, "--headed"], timeout=10)

    def _press(self, key):
        self._run(["press", key, "--app", self.APP, "--headed"], timeout=10)

    def _restore(self):
        """把微信窗口放回可见区（agent-desktop 能读 offscreen 树但不能操作，操作前必须 restore）。"""
        try:
            self._run(["restore", "--app", self.APP], timeout=10, require_ok=False)
        except Exception:
            pass
        try:
            self._run(["move-window", "--app", self.APP, "--x", "0", "--y", "0"], timeout=10, require_ok=False)
        except Exception:
            pass

    def _verify_current_chat(self, expected):
        """点完搜索结果后，验证当前真的进入了 expected 会话。

        做法：群聊界面的输入框 role=textfield 且 name=目标群名，直接搜它。
        """
        try:
            ref = self._find(role="textfield", name=expected, exact=True)
            self.log(f"[agent-desktop] 输入框 ref={ref}, 目标群={expected!r}")
            return ref is not None
        except Exception as e:
            self.log(f"[agent-desktop] 验证当前会话异常: {e}")
            return False

    # ---------- 对外接口 ----------

    def is_logged_in(self):
        """Weixin.exe 进程活着就算在线。"""
        try:
            data = self._run(["list-apps"], timeout=10, require_ok=False)
            apps = data.get("apps") or data.get("applications") or []
            for a in apps:
                name = (a.get("name") or a.get("id") or "").lower()
                if "weixin" in name or "wechat" in name:
                    return True
            # 兜底：tasklist
            out = _run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH", "/FO", "CSV"], timeout=5)
            return "Weixin.exe" in out
        except Exception as e:
            self.log(f"[agent-desktop] is_logged_in 检查失败: {e}")
            return False

    def send_to_group(self, group, text, retries=2):
        """发送文本到指定群。"""
        self.log(f"[agent-desktop] 开始发群「{group}」")
        self._restore()

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                self._send_once(group, text)
                self.log(f"[agent-desktop] 已发送 → {group}")
                return
            except Exception as e:
                last_err = str(e)
                self.log(f"[agent-desktop] 第 {attempt}/{retries} 次失败: {last_err}")
                time.sleep(3)
        raise RuntimeError(f"agent-desktop 发送「{group}」重试 {retries} 次均失败: {last_err}")

    def _send_once(self, group, text):
        """单次发送流程。"""
        # 1. 点击左侧"微信"tab 回到聊天列表
        wechat_tab = self._find(role="button", name="微信", exact=True)
        if wechat_tab:
            self._click(wechat_tab)
            time.sleep(0.5)

        # 2. 点击搜索框
        search_ref = self._find(role="textfield", name="搜索", exact=True)
        if not search_ref:
            # 兜底：按角色模糊
            search_ref = self._find(role="textfield")
        if not search_ref:
            raise RuntimeError("找不到微信搜索框")
        self._click(search_ref)
        time.sleep(0.3)

        # 3. 清空 + 输入群名
        try:
            self._clear(search_ref)
        except Exception:
            pass
        self._type(search_ref, group)
        time.sleep(0.3)
        # 必须按回车触发微信搜索！SetValue 不会自动弹出搜索结果下拉
        self._press("return")
        time.sleep(1.5)  # 等搜索结果加载

        # 4. 找搜索结果里的群项（优先用 native_id 精准定位，兜底用 text 模糊匹配）
        result_ref = None
        # native_id 格式：session_item_群名（微信搜索结果列表里的唯一 ID）
        native_id = f"session_item_{group}"
        for _ in range(10):
            result_ref = (
                self._find(native_id=native_id)
                or self._find(role="option", text=group, exact=True)
                or self._find(role="option", name=group, exact=True)
            )
            if result_ref:
                break
            time.sleep(0.3)

        if not result_ref:
            raise RuntimeError(f"搜索「{group}」未出现匹配结果")

        self.log(f"[agent-desktop] 找到群项 ref={result_ref}，点击进入")
        self._click(result_ref)
        # 微信从搜索结果界面切到群聊界面需要 2-3 秒，等够再验证
        input_ref = None
        for wait_s in (0.5, 1.0, 2.0, 3.0):
            time.sleep(wait_s)
            input_ref = self._find(role="textfield", name=group, exact=True)
            if input_ref:
                break
        if not input_ref:
            raise RuntimeError(
                f"点击搜索结果后 3 秒内未能进入「{group}」群聊界面（输入框未出现），已停止发送以防发错人"
            )

        self.log(f"[agent-desktop] 已进入「{group}」，输入框 ref={input_ref}")

        # 6. 粘贴文本 + 回车发送
        try:
            self._clear(input_ref)
        except Exception:
            pass
        self._type(input_ref, text)
        time.sleep(0.5)
        self._press("return")
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# RpaClient —— wechatauto UIA（历史方案，窗口被最小化就崩）
# ---------------------------------------------------------------------------

class RpaClient:
    """RPA 备用引擎：wechatauto UIA 操作微信 4.x"""

    def __init__(self, window_names=None, log=print):
        self.window_names = list(window_names or ("微信", "WeChat"))
        self.log = log
        self._gui = None
        self._uia = None

    def _engine(self):
        if self._gui is None:
            from wechatauto.guia import WeChatGUI
            self._gui = WeChatGUI()
        if self._uia is None:
            self._uia = self._gui._get_uia()
        if self._uia is None:
            raise RuntimeError("UIA 引擎不可用（无障碍门热激活失败）")
        return self._gui, self._uia

    def is_logged_in(self):
        import ctypes
        import subprocess
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        def has(image):
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return image in (r.stdout or b"").decode("utf-8", "ignore")

        if not (has("Weixin.exe") or has("WeChat.exe")):
            return False

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

    def _ensure_chat(self, uia, group, timeout=10):
        cur = uia.current_chat()
        self.log(f"[wechat] 当前会话={cur!r}，目标={group!r}")
        try:
            ok = uia.open_chat(group, section="ChatRoom")
        except TypeError as e:
            raise RuntimeError('依赖不支持群类型过滤') from e
        if not ok:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if uia.current_chat() == group:
                return True
            time.sleep(0.4)
        return False

    def send_to_group(self, group, text, retries=2):
        gui, uia = self._engine()
        from wechatauto.param import WxParam
        WxParam.ENABLE_OCR = False
        last_err = None
        for i in range(1, retries + 1):
            try:
                if not self._ensure_chat(uia, group):
                    raise RuntimeError(f"未能确认当前会话为「{group}」")
                if not uia.send_text(text):
                    raise RuntimeError("send_text 未确认")
                self.log(f"[wechat] 已发送 → {group}")
                return
            except Exception as e:
                last_err = str(e)
                self.log(f"[wechat] 第 {i}/{retries} 次失败: {last_err}")
                time.sleep(2)
        raise RuntimeError(f"发送到「{group}」重试 {retries} 次均失败: {last_err}")


# ---------------------------------------------------------------------------
# WcferryClient —— 微信 3.9.12.x hook 注入
# ---------------------------------------------------------------------------

class WcferryClient:
    """Wcferry hook 注入引擎（微信 3.9.12.x，API 级不依赖窗口）"""

    def __init__(self, window_names=None, log=print):
        self.log = log
        try:
            import wcferry  # noqa: F401
            self._available = True
        except Exception:
            self._available = False

    def is_logged_in(self):
        return self._available

    def send_to_group(self, group, text, retries=1):
        if not self._available:
            raise RuntimeError("wcferry 不可用（需微信 3.9.12.x）")
        # 极简实现：直接调用 wcferry API 发群
        import wcferry
        wcf = wcferry.Wcf()
        try:
            wcf.send_text(text, f"{group}@chatroom")
            self.log(f"[wcferry] 已发送 → {group}")
        finally:
            wcf.cleanup()


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def make_client(window_names=None, log=print):
    engine = _engine_name()
    log(f"[engine] 使用 {engine} 引擎")
    if engine == "agent-desktop":
        return AgentDesktopClient(window_names=window_names, log=log)
    if engine == "wcferry":
        return WcferryClient(window_names=window_names, log=log)
    if engine == "rpa":
        return RpaClient(window_names=window_names, log=log)
    raise RuntimeError(f"未知引擎: {engine}")


def com_init():
    """wechatauto UIA 的 COM 初始化（只给 RpaClient 用）。

    agent-desktop / wcferry 不需要 COM 初始化，所以这个函数是空壳。
    保留它是因为 send_once.py 会 import com_init，即使 agent-desktop 不走 rpa 分支。
    """
    try:
        import uiautomation as auto
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass
