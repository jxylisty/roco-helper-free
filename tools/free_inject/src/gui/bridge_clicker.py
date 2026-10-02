# -*- coding: utf-8 -*-
"""bridge_clicker.py — 鼠标连点器 (免费版专属模块)

定位: 工具箱页签内的纯前端参数面板 + 后端线程循环点击。
执行层: ctypes 直调 user32.SendInput —— Windows 输入栈最底层的
    硬件级注入(与真实鼠标事件同队列), 零第三方依赖, 不依赖
    pyautogui/keyboard 等可选库。仅注入鼠标事件, 不含按键序列、
    不含图像识别、不含自动决策 —— 与主仓付费自动化引擎无任何代码关联。

能力:
  - 单点/双击/按下保持/右键/中键, CPS 精细调速(0.1 步进)
  - 固定 + 随机抖动间隔(拟人节奏, 抖动幅度可调)
  - 最多 8 个坐标点位循环(抓取任意窗口屏幕坐标), 支持循环圈数/次数
  - 全局热键 F7 启停 / F8 急停(keyboard 库, 免费树 requirements 已含)
  - 运行状态实时回报(已点次数/当前点位/异常计数)
"""
import ctypes
import random
import threading
import time
from ctypes import wintypes

# ---------------- SendInput 原生定义 ----------------

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
INPUT_MOUSE = 0

# 绝对坐标归一化到 0..65535
_COORD_NORM = 65535


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_user32 = ctypes.windll.user32


def _send_mouse(flags: int, x: int = 0, y: int = 0) -> None:
    """发送一组鼠标输入事件; 绝对坐标用像素(x,y 传 None 表示相对/纯按钮事件)"""
    inp = _INPUT(type=INPUT_MOUSE)
    if x is not None:
        vx = int(x * _COORD_NORM / max(1, _user32.GetSystemMetrics(0)))
        vy = int(y * _COORD_NORM / max(1, _user32.GetSystemMetrics(1)))
        inp.dx = vx
        inp.dy = vy
        inp.dwFlags = flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
    else:
        inp.dwFlags = flags
    inp.mouseData = 0
    inp.time = 0
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _screen_size():
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


# ---------------- 点击器服务 ----------------

# 热键常量(与免费树全局热键 F4/F9/F10 体系错开)
HOTKEY_TOGGLE = "f7"
HOTKEY_KILL = "f8"

# 单点位支持的动作
_ACTIONS = ("left", "double", "right", "middle", "down")


class ClickerService:
    """纯后台线程连点器: 状态字段由 RPC 线程安全读写

    Mixin 的 __init__ 不会被执行(与 bridge_merchant 同款约束),
    状态字段统一在 clicker_init() 里初始化, 由 AppBridge.__init__ 调用。
    """

    def __init__(self, log=None):
        self._clicker_log = log or (lambda msg, level="info": None)

    # ---------- 初始化(AppBridge.__init__ 显式调用) ----------

    def clicker_init(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_evt = threading.Event()
        self._kill_evt = threading.Event()   # 热键急停: 跳过善后立即退出
        # 运行配置(clicker_start 参数快照)
        self._points = []                    # [{"x":..,"y":..,"action":"left","cps":..,"repeat":..}, ...]
        self._loops = 0                      # 0 = 无限
        self._jitter = 0.0                   # 间隔抖动比例 0..1
        self._restore_pos = True             # 结束后鼠标回位
        # 运行时状态
        self._running = False
        self._clicks = 0
        self._cur_point = -1
        self._started_at = None

    def _ensure_init(self):
        """RPC 先于 clicker_init 到达时兜底(幂等)"""
        if not hasattr(self, "_lock"):
            self.clicker_init()

    def _log(self, msg: str, level: str = "info"):
        try:
            self._clicker_log(msg, level)
        except Exception:
            pass

    # ---------- 状态快照 ----------

    def _status(self, success=True, message="") -> dict:
        with self._lock:
            return {
                "success": success,
                "message": message,
                "running": self._running,
                "clicks": self._clicks,
                "cur_point": self._cur_point,
                "points": len(self._points),
                "loops": self._loops,
                "elapsed": round(time.time() - self._started_at, 1) if self._started_at else 0,
            }

    # ---------- RPC: clicker_cursor ----------

    def clicker_cursor(self) -> dict:
        """抓取当前光标屏幕坐标(原子读, 供前端「抓取鼠标位置」用)"""
        try:
            class _PT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
            pt = _PT()
            if _user32.GetCursorPos(ctypes.byref(pt)):
                return {"success": True, "x": int(pt.x), "y": int(pt.y)}
        except Exception as e:
            return {"success": False, "message": str(e)}
        return {"success": False, "message": "GetCursorPos 失败"}

    # ---------- RPC: clicker_status ----------

    def clicker_status(self) -> dict:
        self._ensure_init()
        return self._status()

    # ---------- RPC: clicker_start ----------

    def clicker_start(self, points=None, loops=0, jitter=0.0,
                      restore_pos=True, hotkeys=None) -> dict:
        """points: [{x,y,action,cps,repeat}, ...] 每点位独立动作与速率"""
        self._ensure_init()
        with self._lock:
            if self._running:
                return {"success": False, "message": "连点器已在运行"}
            pts = []
            for p in (points or []):
                try:
                    x = int(p.get("x", 0)); y = int(p.get("y", 0))
                    action = str(p.get("action", "left")).lower()
                    cps = float(p.get("cps", 8))
                    repeat = int(p.get("repeat", 0) or 0)
                except (TypeError, ValueError):
                    return {"success": False, "message": "点位参数格式错误"}
                if action not in _ACTIONS:
                    return {"success": False, "message": f"未知动作: {action}"}
                if cps < 0.5 or cps > 100:
                    return {"success": False, "message": "CPS 需在 0.5 ~ 100 之间"}
                sw, sh = _screen_size()
                if not (0 <= x < sw and 0 <= y < sh):
                    return {"success": False,
                            "message": f"坐标越界: ({x},{y}) 超出屏幕 {sw}x{sh}"}
                pts.append({"x": x, "y": y, "action": action,
                            "cps": min(100.0, max(0.5, cps)), "repeat": max(0, repeat)})
            if not pts:
                return {"success": False, "message": "至少需要 1 个点击点位"}
            self._points = pts[:8]  # 上限 8 点位
            try:
                self._loops = max(0, int(loops or 0))
                self._jitter = min(0.8, max(0.0, float(jitter or 0)))
            except (TypeError, ValueError):
                return {"success": False, "message": "圈数/抖动参数格式错误"}
            self._restore_pos = bool(restore_pos)
            self._clicks = 0
            self._cur_point = -1
            self._started_at = time.time()
            self._stop_evt.clear()
            self._kill_evt.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="clicker-worker", daemon=True)
            self._thread.start()
        self._log(f"连点器启动: {len(self._points)} 个点位 · "
                  f"{'无限循环' if not self._loops else f'{self._loops} 圈'}", "success")
        return self._status(message="连点器已启动")

    # ---------- RPC: clicker_stop ----------

    def clicker_stop(self, kill: bool = False) -> dict:
        self._ensure_init()
        with self._lock:
            if not self._running:
                return {"success": False, "message": "连点器未在运行"}
            if kill:
                self._kill_evt.set()
            self._stop_evt.set()
        # 等线程退出(急停最多 0.5s, 常规停给足一个点击周期)
        evt = self._kill_evt if kill else self._stop_evt
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0 if not kill else 0.5)
        self._log("连点器已停止" + (" (急停)" if kill else ""), "warning" if kill else "info")
        return self._status(message="已停止")

    # ---------- 执行线程 ----------

    def _run(self):
        try:
            self._loop()
        finally:
            with self._lock:
                self._running = False
                self._cur_point = -1

    def _loop(self):
        idle = threading.Event()
        while not self._stop_evt.is_set() and not self._kill_evt.is_set():
            for idx, pt in enumerate(self._points):
                if self._stop_evt.is_set() or self._kill_evt.is_set():
                    return
                with self._lock:
                    self._cur_point = idx
                repeats = pt["repeat"] if pt["repeat"] > 0 else 1
                for _ in range(repeats):
                    if self._stop_evt.is_set() or self._kill_evt.is_set():
                        return
                    self._click_at(pt)
                    base = 1.0 / pt["cps"]
                    if self._jitter > 0:
                        base *= 1.0 + random.uniform(-self._jitter, self._jitter)
                    idle.wait(min(base, 2.0))  # 分片等待, 急停响应 < 2s
            if self._loops > 0:
                self._loops -= 1
                if self._loops <= 0:
                    return

    def _click_at(self, pt: dict):
        x, y, action = pt["x"], pt["y"], pt["action"]
        try:
            if action == "left":
                _send_mouse(MOUSEEVENTF_LEFTDOWN, x, y)
                _send_mouse(MOUSEEVENTF_LEFTUP)
            elif action == "double":
                for _ in range(2):
                    _send_mouse(MOUSEEVENTF_LEFTDOWN, x, y)
                    _send_mouse(MOUSEEVENTF_LEFTUP)
                    time.sleep(0.06)  # 系统双击间隔下限
            elif action == "right":
                _send_mouse(MOUSEEVENTF_RIGHTDOWN, x, y)
                _send_mouse(MOUSEEVENTF_RIGHTUP)
            elif action == "middle":
                _send_mouse(MOUSEEVENTF_MIDDLEDOWN, x, y)
                _send_mouse(MOUSEEVENTF_MIDDLEUP)
            elif action == "down":
                _send_mouse(MOUSEEVENTF_LEFTDOWN, x, y)  # 按下保持(由 stop 释放)
            with self._lock:
                self._clicks += 1
        except Exception:
            pass

    # ---------- 全局热键(可选: keyboard 库缺席时静默降级) ----------

    def install_hotkeys(self, toggle=None, kill=None):
        """注册全局热键; keyboard 不可用时不影响主功能"""
        self._ensure_init()
        toggle = toggle or HOTKEY_TOGGLE
        kill = kill or HOTKEY_KILL

        def _toggle():
            if self._running:
                self.clicker_stop()
            else:
                # 热键启动: 复用最近一次配置(无配置则忽略)
                if self._points:
                    self._stop_evt.clear()
                    self._kill_evt.clear()
                    with self._lock:
                        self._clicks = 0
                        self._started_at = time.time()
                        self._running = True
                    self._thread = threading.Thread(
                        target=self._run, name="clicker-worker", daemon=True)
                    self._thread.start()
                    self._log("连点器热键启动(沿用上次配置)", "success")

        try:
            import keyboard
            keyboard.add_hotkey(toggle, _toggle, suppress=False)
            keyboard.add_hotkey(kill, lambda: self.clicker_stop(kill=True),
                                suppress=False)
            return True
        except Exception:
            return False
