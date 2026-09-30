# -*- coding: utf-8 -*-
"""sync_free.py — 从主仓生成免费版代码树（洛克小助手 Free, manifest 驱动）

用法:
    python tools/sync_free.py --main <主仓路径> --out <输出目录> [--dry-run]

流程: 拷贝主仓 → 应用排除清单 → 强制包含免费依赖 → 写入付费闸门存根
     → 前端 index.html/app.js 注入 FREE_BUILD 补丁 → 产出 Free 树。
状态: v0.3 拆分定稿（2026-09-30 与作者对齐）:
  免费(噱头): PVP 识别引擎 + AI 军师悬浮窗 + AI 设置 + 赛季战报(本机)
  付费(锁页): 丢球助手/挂机引擎页 + AI 对战(接管/MCP) + 日常任务
  删除(无导航): 视觉调试台 + 工具箱 + 背包盘点 + 配置中心 + PVE 数据层
分发前必做:
  1) --dry-run 核对排除清单没漏主仓新增的付费/敏感文件
  2) python tools/check_imports.py <dist_free> 复查导入图
  3) compileall 冒烟 + 实机启动验证
本脚本与产出树永不包含密钥、抓包与授权核心逻辑（见 docs/功能拆分.md）。
"""
import argparse
import shutil
from pathlib import Path

# 免费版会员引导 QQ（小鲸鱼）
FREE_QQ = "3808239548"

# ---------------- 排除清单 ----------------
# 目录级排除（相对主仓根）:
#   以 "/" 结尾 = 根级目录前缀（只排顶层, 不伤子目录同名）
#   带斜杠     = POSIX 前缀（相对主仓根）
#   简单名     = 任意层级路径段命中即排除
EXCLUDE_DIRS = [
    "server/",           # 顶层授权服务端（worker.js 等; src/server 是 FastAPI 本体, 保留）
    "src/capture/",      # 抓包 + RKPP + 截屏后端（免费军师走 OCR 源; 仅强制包含窗口截屏三件套）
    "src/tasks/",        # 日常任务执行器（付费）
    "src/gui/studio/",   # 视觉工坊独立前端（开发者）
    "build", "dist",     # 构建产物
    ".git", "__pycache__", ".vscode", ".idea", ".zcode",
    ".venv", "venv",               # 虚拟环境
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "htmlcov",
    "node_modules", ".github",
    "data/",             # 运行数据（历史库/截图/抓包缓存）; 注意 src/pvp/data 是竞技数据, 不匹配
    "data/config/",      # 根级运行配置（含用户 settings; ROI 模板由 INCLUDE_FORCE 带入）
    "docs",              # 主仓内部文档（本仓库有自己的 docs）
    "output",            # 对局截图/敌方头像/OCR 失败帧（含用户对局数据）
    "src-tauri/target", "src-tauri/gen",
    "tools",             # 主仓 tools = 构建链/调试脚本/抓包工具(免费仓有自己的 tools)
    "scripts",
]

# 文件级排除（相对主仓根的 POSIX 路径）
EXCLUDE_FILES = [
    # 根目录调试残留
    "_exp_battle_events.json",
    "_shiny_exp_events.json",
    "_shiny_pair.json",
    # 授权体系（免费版换群口令验证存根）
    "src/gui/auth.py",
    "src/gui/auth_core.py",
    "src/gui/auth_core.pyd",
    "src/gui/bridge_auth.py",
    # 抓包工具与测试
    "tests/test_capture.py",
    # 构建链与密钥
    "tools/build_hardened.py",
    "tools/gen_worker_keys.js",
    "tools/pvp_mcp_server.py",
    "keys.json",
]

# 强制包含（位于被排除目录内, 但免费功能依赖的文件; 优先于目录排除）
# 依据: 军师 OCR 管线需要窗口截屏; ROI 模板是识别坐标系, 无敏感数据
INCLUDE_FORCE = [
    "src/capture/__init__.py",        # 仅 docstring
    "src/capture/window_capture.py",  # win32 窗口截屏, 无 src.* 依赖
    "src/capture/fast_capture.py",    # mss 快速截屏, 无 src.* 依赖
    "src/capture/snapshot_adapter.py",  # 采集源适配层(纯翻译, 无抓包; OCR 路径引用)
    "data/config/roi_templates/*.json",  # ROI 坐标模板(仅 json, 不带用户截图 png)
]

# 强制包含的目录: 递归带入整目录（INCLUDE_FORCE 中以目录形式列出的条目）
INCLUDE_FORCE_DIRS = [e for e in INCLUDE_FORCE if not e.endswith(".py")]

# ---------------- 主仓源文件补丁（正则原文替换, 逐条可回溯） ----------------
# 每条: (文件, 旧文本, 新文本, 说明)。命中失败会报错退出, 防止静默漂移。
SOURCE_PATCHES = [
    # 1) 免费版强制用户模式: DEV 页(视觉调试台)与开发者功能全隐藏
    #    (主仓逻辑: 源码运行默认 dev; 打包才强制用户版 → 免费树是源码分发, 必须显式钉死)
    ("src/gui/bridge_common.py",
     'if getattr(sys, "frozen", False):\n    DEV_MODE = False\nelse:\n    _env_dev = os.environ.get("LKW_DEV_MODE")\n    DEV_MODE = (_env_dev != "0")',
     'DEV_MODE = False  # [FREE BUILD] 免费版强制用户模式: 隐藏视觉调试台等开发者入口',
     "钉死 DEV_MODE=False"),
    # 2) 丢球热键不注册: F4/F9/F10 是付费丢球功能的全局热键
    ("auto_throw_ball.py",
     "        keyboard.add_hotkey('f4', self.toggle)\n        keyboard.add_hotkey('f9', self.toggle_bomber)\n        keyboard.add_hotkey('f10', self.toggle_skill)\n        self._log(\"快捷键已注册: F4 普通丢球 / F9 轰炸机 / F10 技能\", \"info\")",
     "        # [FREE BUILD] 丢球/轰炸/技能为 Pro 功能, 不注册热键\n        self._log(\"丢球热键为 Pro 功能, 未注册\", \"info\")",
     "摘除 F4/F9/F10 热键"),
    # 3) 窗口级 F8 截图热键同样指向付费调试链路, 摘除
    ("src/gui/bridge_widget.py",
     "            keyboard.add_hotkey('f8', self._hotkey_snip)\n            keyboard.add_hotkey('f11', self._emergency_stop)",
     "            # [FREE BUILD] F8 截图属 Pro 功能, 不注册; F11 急停保留\n            keyboard.add_hotkey('f11', self._emergency_stop)",
     "摘除 F8 截图热键"),
    # 4) 视觉工坊目录已排除, studio 静态挂载改为条件挂载(目录缺失不崩)
    ("src/server/app.py",
     'app.mount("/studio", StaticFiles(directory=str(STUDIO_DIR)), name="studio")',
     'if STUDIO_DIR.exists():  # [FREE BUILD] 视觉工坊不入免费树, 缺失时跳过挂载\n    app.mount("/studio", StaticFiles(directory=str(STUDIO_DIR)), name="studio")',
     "studio 挂载条件化"),
    # 5) 免费版无 git 通道, 不启动 GitHub 更新器(bridge.py 顶层导入 updater 保留无害,
    #    但 AutoUpdater 线程会真的去 fetch origin → 免费树连本地提交都没有, 静默失败无意义)
    ("src/gui/bridge.py",
     '        # 自动更新器: 后台静默检查 GitHub 新版本(仅提示, 不自动改文件)\n        self._update_hint_sent = None\n        self.updater = AutoUpdater(on_update_available=self._notify_update_available)',
     '        # [FREE BUILD] 免费版更新走 QQ 群分发, 不启动 GitHub 更新器\n        self._update_hint_sent = None\n        self.updater = None',
     "免费版停用 GitHub 更新器"),
    # 6) 付费丢球三开关补闸门: toggle_normal/bomber/skill 直调 tool 未走鉴权,
    #    热键摘除后这是绕过 _auth_gate 的最后一条 RPC 通路(F4/F9/F10 已不注册)
    ("src/gui/bridge_runtime.py",
     '    def toggle_normal(self) -> dict:\n        will_start = not self.tool.running',
     '    def toggle_normal(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 丢球为付费功能\n        if gate:\n            return gate\n        will_start = not self.tool.running',
     "toggle_normal 补闸门"),
    ("src/gui/bridge_runtime.py",
     '    def toggle_bomber(self) -> dict:\n        will_start = not self.tool.bomber_running',
     '    def toggle_bomber(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 轰炸机为付费功能\n        if gate:\n            return gate\n        will_start = not self.tool.bomber_running',
     "toggle_bomber 补闸门"),
    ("src/gui/bridge_runtime.py",
     '    def toggle_skill(self) -> dict:\n        will_start = not self.tool.skill_running',
     '    def toggle_skill(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 自动技能为付费功能\n        if gate:\n            return gate\n        will_start = not self.tool.skill_running',
     "toggle_skill 补闸门"),
    # 7) 摘除 F8 后启动日志文案同步(避免免费版日志谎称 F8 可用)
    ("src/gui/bridge_widget.py",
     '            print("[热键] keyboard 库注册完成: F8截图/F11急停", flush=True)\n            self._enqueue_log("快捷键: F8截图/F11急停", "info")',
     '            print("[热键] keyboard 库注册完成: F11急停", flush=True)  # [FREE BUILD] F8 属付费\n            self._enqueue_log("快捷键: F11急停", "info")',
     "F8 文案同步"),
    # 8) 背包盘点(抓包计球, 辅助丢球)属付费: bag_open 是真实按键自动化, 必须硬闸
    ("src/gui/bridge_pvp_data.py",
     '    def bag_scan(self) -> dict:\n        """背包盘点: 优先协议直读',
     '    def bag_scan(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包盘点为付费功能\n        if gate:\n            return gate\n        """背包盘点: 优先协议直读',
     "bag_scan 补闸门"),
    ("src/gui/bridge_pvp_data.py",
     '    def bag_open(self) -> dict:\n        """置顶游戏',
     '    def bag_open(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包打开为付费自动化\n        if gate:\n            return gate\n        """置顶游戏',
     "bag_open 补闸门"),
    ("src/gui/bridge_pvp_data.py",
     '    def bag_open_and_scan(self) -> dict:\n        """打开背包',
     '    def bag_open_and_scan(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包盘点为付费功能\n        if gate:\n            return gate\n        """打开背包',
     "bag_open_and_scan 补闸门"),
    # 9) 锁幕选择器与拆分线对齐: 主仓锁 pvp(付费), 免费版 pvp 是噱头必须放开, 改锁 aipvp
    #    (aipvp 实际被 switchPage 拦截弹会员引导, 锁幕只是直连兜底)
    ("src/gui/web/assets/app.js",
     "    document.querySelectorAll('#page-daily, #page-pvp').forEach(page => {",
     "    document.querySelectorAll('#page-daily, #page-aipvp').forEach(page => {  // [FREE BUILD] pvp 免费",
     "锁幕选择器对齐拆分线"),
]

# ---------------- 存根 ----------------
# 免费树中的付费闸门: AuthUpdateMixin 是唯一闸门实现, 其余 Mixin 全量带入真文件。
# _auth_gate 返回 {"auth_required": True} → 前端 FREE_BUILD 补丁据道拦截并弹会员引导。
STUBS = {
    "src/gui/bridge_auth.py": (
        '"""[Free 构建存根] 免费版授权: 付费闸门统一指向会员引导, 无卡密体系。"""\n\n\n'
        f'FREE_QQ = "{FREE_QQ}"  # 会员购买引导(小鲸鱼)\n\n\n'
        'class AuthUpdateMixin:\n'
        '    """Free 构建授权存根: 闸门=会员引导, 账号接口=免费版提示。"""\n\n'
        '    def _auth_gate(self) -> dict | None:\n'
        '        """付费功能硬闸: 全部拒绝并引导开通会员(前端据此弹引导页)。"""\n'
        '        return {"ok": False, "auth_required": True, "free_build": True,\n'
        '                "message": "该功能为付费版功能, 请开通会员", "pro_qq": FREE_QQ}\n\n'
        '    def auth_status(self) -> dict:\n'
        '        return {"ok": True, "authorized": False, "dev_mode": False,\n'
        '                "free_build": True, "pro_qq": FREE_QQ,\n'
        '                "nickname": "免费版", "expires_at": 0}\n\n'
        '    def auth_activate(self, *a, **k):\n'
        '        return {"ok": False, "free_build": True,\n'
        '                "message": f"免费版无卡密体系, 开通会员请联系 QQ: {FREE_QQ}"}\n\n'
        '    def auth_logout(self):\n'
        '        return {"ok": True, "message": "免费版无需退出"}\n\n'
        '    def auth_verify_remote(self):\n'
        '        return {"ok": True, "authorized": False, "free_build": True}\n\n'
        '    def start_auth_verify(self):\n'
        '        pass\n\n'
        '    def _push_auth_state(self):\n'
        '        pass\n\n'
        '    # ---- 免费版更新: 无 git 通道, 统一指向 QQ 群 ----\n'
        '    _update_hint = None\n\n'
        '    def _notify_update_available(self, info):\n'
        '        pass  # 免费版不启动更新器, 此回调不会触发\n\n'
        '    def update_check(self):\n'
        '        return {"success": False, "free_build": True,\n'
        '                "message": f"免费版请在 QQ 群获取最新版 (群主 QQ: {FREE_QQ})"}\n\n'
        '    def update_apply(self):\n'
        '        return {"success": False, "free_build": True,\n'
        '                "message": f"免费版请从 QQ 群重新下载 (群主 QQ: {FREE_QQ})"}\n\n'
        '    def update_status(self):\n'
        '        return {"success": True, "free_build": True, "has_update": False}\n\n'
        '    def auth_account_login(self, *a, **k):\n'
        '        return {"ok": False, "free_build": True,\n'
        '                "message": f"免费版无需登录, 开通会员请联系 QQ: {FREE_QQ}"}\n\n'
        '    def auth_account_register(self, *a, **k):\n'
        '        return {"ok": False, "free_build": True, "message": "免费版无需注册"}\n\n'
        '    def auth_account_sendcode(self, *a, **k):\n'
        '        return {"ok": False, "free_build": True, "message": "免费版无需验证"}\n\n'
        '    def auth_account_reset(self, *a, **k):\n'
        '        return {"ok": False, "free_build": True, "message": "免费版无需重置"}\n\n'
        '    def free_verify_group(self, qq="", group_key="", hwid=""):\n'
        '        """免费版入群验证: QQ号+群口令 → worker /free/verify 校验。\n'
        '        hwid 由前端取机器指纹; token 落地 data/config/free_auth.json 后离线放行。"""\n'
        '        import json as _json\n'
        '        import urllib.request as _ur\n'
        '        cfg_dir = __import__("src.gui.bridge_common", fromlist=["CONFIG_DIR"]).CONFIG_DIR\n'
        '        token_file = cfg_dir / "free_auth.json"\n'
        '        if token_file.exists():\n'
        '            try:\n'
        '                st = _json.loads(token_file.read_text(encoding="utf-8"))\n'
        '                if st.get("token") and st.get("qq") == qq:\n'
        '                    return {"ok": True, "verified": True, "nickname": st.get("nickname", ""),\n'
        '                            "message": "本机已验证"}\n'
        '            except Exception:\n'
        '                pass\n'
        '        if not (qq and group_key and hwid):\n'
        '            return {"ok": False, "verified": False, "message": "请填写 QQ 号和群口令"}\n'
        '        try:\n'
        '            req = _ur.Request(\n'
        '                "https://lucky-cell-cd0b.zzx051012-e82.workers.dev/free/verify",\n'
        '                data=_json.dumps({"qq": qq, "group_key": group_key, "hwid": hwid}).encode(),\n'
        '                headers={"Content-Type": "application/json"})\n'
        '            with _ur.urlopen(req, timeout=15) as resp:\n'
        '                data = _json.loads(resp.read().decode("utf-8"))\n'
        '            if not data.get("ok"):\n'
        '                return {"ok": False, "verified": False, "message": data.get("message", "验证失败")}\n'
        '            cfg_dir.mkdir(parents=True, exist_ok=True)\n'
        '            token_file.write_text(_json.dumps(\n'
        '                {"qq": qq, "token": data.get("token", ""),\n'
        '                 "nickname": data.get("nickname", ""), "verified_at": __import__("time").time()},\n'
        '                ensure_ascii=False), encoding="utf-8")\n'
        '            return {"ok": True, "verified": True, "nickname": data.get("nickname", ""),\n'
        '                    "message": "验证通过, 感谢支持正版"}\n'
        '        except Exception as e:\n'
        '            return {"ok": False, "verified": False, "message": f"验证服务不可达: {e}"}\n'
    ),
    # 免费树 src/pvp 用主仓真 __init__(re-export 的模块全部随 src/pvp 整体带入), 无需存根
}

# ---------------- 免费版前端补丁 ----------------
# index.html: <title> 定名 + </body> 前 FREE_BUILD 脚本(付费页拦截 + 会员引导弹窗)
# app.js: NAV_PAID_PAGES 从 ['pvp','aipvp','daily'] 改为 ['auto','aipvp','daily']
#         (免费噱头 = pvp 页; 付费 = 丢球/挂机页 + AI 对战接管 + 日常)
FREE_PATCH_HTML = """<script>
/* [FREE BUILD] 免费版补丁: 付费页拦截 + 会员引导 */
window.FREE_BUILD = true;
window.FREE_QQ = 'FREE_QQ_PLACEHOLDER';
(function () {
    'use strict';
    // 付费页: 丢球助手(auto)/AI 对战(aipvp)/日常(daily) — RPC 层已硬闸, 这里拦导航
    var PAID_PAGES = { auto: '丢球助手 / 挂机引擎', aipvp: 'AI 对战 (自动接管)', daily: '日常任务托管' };
    var PRO_QQ = window.FREE_QQ;
    function showProModal(label) {
        var ov = document.getElementById('customModalOverlay');
        var t = document.getElementById('customModalTitle');
        var d = document.getElementById('customModalDesc');
        var btn = document.getElementById('customModalConfirmBtn');
        var cancel = document.getElementById('customModalCancelBtn');
        var wrap = document.getElementById('customModalInputWrap');
        if (!ov || !t || !d) { if (window.showToast) showToast(label + ' 为付费版功能, 请开通会员', 'warning'); return; }
        t.textContent = '付费版功能';
        d.innerHTML = '<b>' + label + '</b> 为付费版功能。<br>免费版包含: PVP 对战识别 · AI 军师 · 赛季战报 · AI 设置。<br>'
            + '开通会员请联系 QQ: <b id="freeProQq" style="cursor:pointer;user-select:all">' + PRO_QQ + '</b> (点击复制)';
        if (wrap) wrap.style.display = 'none';
        if (btn) { btn.textContent = '复制 QQ 号'; btn.style.display = ''; }
        if (cancel) cancel.textContent = '关闭';
        ov.classList.add('show');
        if (btn) btn.onclick = function () {
            var done = function () { if (window.showToast) showToast('QQ 号已复制: ' + PRO_QQ, 'success'); };
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(PRO_QQ).then(done, done);
            else done();
        };
        var qqEl = document.getElementById('freeProQq');
        if (qqEl) qqEl.onclick = function () {
            if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(PRO_QQ).then(function(){ if(window.showToast) showToast('QQ 号已复制', 'success'); }, function(){});
        };
    }
    window.showProModal = showProModal;
    // 拦截 switchPage: 付费页不进入, 弹引导
    if (typeof window.switchPage === 'function') {
        var _sw = window.switchPage;
        window.switchPage = function (name) {
            if (PAID_PAGES[name]) { showProModal(PAID_PAGES[name]); return; }
            return _sw.apply(this, arguments);
        };
    }
    // 导航重排: 付费项沉底 + PRO 徽标(与主仓 applyNavOrder 协同: isAuthed 恒 false)
    function freeNav() {
        var nav = document.querySelector('.nav');
        if (!nav) return;
        Object.keys(PAID_PAGES).forEach(function (p) {
            var b = nav.querySelector('.nav-item[data-page="' + p + '"]');
            if (!b) return;
            if (!b.querySelector('.nav-pro')) {
                var s = document.createElement('span');
                s.className = 'nav-pro'; s.textContent = 'PRO';
                b.appendChild(s);
            }
            nav.appendChild(b);   // 沉底
        });
        // 隐藏已删除页面的导航残留(理论上下游 CSS 已无, 兜底)
        ['vision', 'tools', 'bag', 'config'].forEach(function (p) {
            var b = nav.querySelector('.nav-item[data-page="' + p + '"]');
            if (b) b.style.display = 'none';
        });
        // 编号连续化
        var n = 0;
        nav.querySelectorAll('.nav-item').forEach(function (b) {
            if (b.style.display !== 'none') {
                n += 1;
                var i = b.querySelector('.nav-idx');
                if (i) i.textContent = String(n).padStart(2, '0');
            }
        });
    }
    if (window.applyNavOrder) {
        var _origNav = window.applyNavOrder;
        window.applyNavOrder = function () {
            var r = _origNav.apply(this, arguments);
            freeNav();
            return r;
        };
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', freeNav);
    } else {
        freeNav();
    }
    // 头像点击: 免费版已验证则提示, 未验证弹入群验证
    window.authClick = function () {
        if (window.showProModal) showProModal('会员开通');
    };
    // 免费版身份显示: 主仓 renderAuth 对未授权显示「未登录 · 点击登录」, 免费版改写
    if (typeof window.renderAuth === 'function') {
        var _ra = window.renderAuth;
        window.renderAuth = function () {
            var r = _ra.apply(this, arguments);
            try {
                var sub = document.getElementById('authState');
                var box = document.getElementById('authAvatarBox');
                if (sub) sub.innerHTML = '<span class="auth-exp">免费版 · 点击开通会员</span>';
                if (box) box.title = '免费版 — 点击开通会员';
            } catch (e) { /* ignore */ }
            return r;
        };
    }
    // 免费版无更新通道: 检查更新给出明确提示(主仓 updater 依赖 git)
    window.freeUpdateHint = function () {
        if (window.showToast) showToast('免费版更新请在 QQ 群获取最新版', 'info');
        return { success: false, message: '免费版请在 QQ 群获取更新' };
    };
})();
</script>"""


def _excluded(rel: Path) -> bool:
    """排除判定: 简单名按路径段匹配, 带斜杠按 POSIX 前缀, 根级(尾/)按顶层前缀"""
    posix = rel.as_posix()
    for d in EXCLUDE_DIRS:
        if d.endswith("/"):
            if posix.startswith(d):
                return True
        elif "/" in d:
            if posix == d or posix.startswith(d + "/"):
                return True
        elif d in rel.parts:
            return True
    return posix in EXCLUDE_FILES


def _force_included(rel: Path) -> bool:
    """INCLUDE_FORCE 判定: 文件精确匹配 + 目录前缀(递归带入) + fnmatch 通配"""
    import fnmatch
    posix = rel.as_posix()
    if posix in INCLUDE_FORCE:
        return True
    for d in INCLUDE_FORCE_DIRS:
        if posix.startswith(d + "/"):
            return True
    for pat in INCLUDE_FORCE:
        if any(c in pat for c in "*?["):
            if fnmatch.fnmatch(posix, pat):
                return True
    return False


def iter_source_files(main_root: Path):
    """按排除清单产出 (相对路径, 源文件) 对; INCLUDE_FORCE 优先于目录排除"""
    for p in sorted(main_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(main_root)
        if _force_included(rel):
            yield rel, p
            continue
        if _excluded(rel):
            continue
        yield rel, p


def apply_source_patches(main_root: Path, out_root: Path) -> None:
    """对产出的免费树打源码补丁; 任一条未命中即报错(防主仓漂移后静默失效)"""
    for rel, old, new, why in SOURCE_PATCHES:
        dst = out_root / rel
        text = dst.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"[sync_free] 补丁锚点失效({why}): {rel} 命中 {text.count(old)} 次, 请核对主仓改动")
        dst.write_text(text.replace(old, new), encoding="utf-8")
        print(f"[sync_free] 补丁 OK: {why} ({rel})")


def main() -> None:
    ap = argparse.ArgumentParser(description="从主仓生成免费版代码树")
    ap.add_argument("--main", required=True, help="主仓根目录")
    ap.add_argument("--out", required=True, help="免费树输出目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args()

    main_root = Path(args.main).resolve()
    out_root = Path(args.out).resolve()
    if not main_root.is_dir():
        raise SystemExit(f"主仓不存在: {main_root}")

    copied = [rel for rel, _ in iter_source_files(main_root)]
    stubs = dict(STUBS)
    all_items = copied + [Path(r) for r in stubs]

    print(f"[sync_free] 计划: 拷贝 {len(copied)} 个文件 + {len(stubs)} 个存根 + {len(SOURCE_PATCHES)} 个补丁")
    if args.dry_run:
        for rel in sorted(all_items, key=lambda p: p.as_posix()):
            tag = "STUB" if rel.as_posix() in stubs else "COPY"
            print(f"  {tag} {rel.as_posix()}")
        return

    if out_root.exists():
        shutil.rmtree(out_root)
    for rel, src in iter_source_files(main_root):
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel, content in stubs.items():
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    apply_source_patches(main_root, out_root)

    # 前端补丁: index.html 标题 + 注入 FREE_BUILD 脚本(</body> 前)
    idx_html = out_root / "src" / "gui" / "web" / "index.html"
    if idx_html.exists():
        html = idx_html.read_text(encoding="utf-8")
        html = html.replace("洛克王国 · PVP 助手控制台", "洛克小助手 · Free 控制台")
        if "FREE_BUILD" not in html:
            html = html.replace("</body>", FREE_PATCH_HTML.replace("FREE_QQ_PLACEHOLDER", FREE_QQ) + "\n</body>", 1)
        idx_html.write_text(html, encoding="utf-8")
        print("[sync_free] 前端补丁 OK: index.html")

    # 导航付费清单改写: app.js 的 NAV_PAID_PAGES 与拆分线保持一致
    app_js = out_root / "src" / "gui" / "web" / "assets" / "app.js"
    if app_js.exists():
        js = app_js.read_text(encoding="utf-8")
        old = "const NAV_PAID_PAGES = ['pvp', 'aipvp', 'daily'];"
        new = "const NAV_PAID_PAGES = ['auto', 'aipvp', 'daily']; // [FREE BUILD]"
        if js.count(old) == 1:
            js = js.replace(old, new)
            app_js.write_text(js, encoding="utf-8")
            print("[sync_free] 前端补丁 OK: app.js NAV_PAID_PAGES")
        elif "[FREE BUILD]" in js:
            print("[sync_free] 前端补丁跳过: app.js 已含 FREE 标记")
        else:
            raise SystemExit("[sync_free] app.js 锚点失效: NAV_PAID_PAGES 未命中, 请核对主仓改动")

    # 免费版已删除页面的前端 section/资源保留无妨(导航已拦), 但 pve 数据层不入树已由排除清单保证

    marker = out_root / "FREE_BUILD"
    marker.write_text(
        "洛克小助手 Free 构建产物 — 更新通道: QQ 群获取\n",
        encoding="utf-8")
    print(f"[sync_free] 免费树已生成: {out_root}")
    print("[sync_free] TODO: compileall 冒烟 + check_imports 复查 + 启动验证通过后才可分发")


if __name__ == "__main__":
    main()
