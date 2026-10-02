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
import fnmatch
import shutil
import sys
from pathlib import Path

# Windows 控制台编码保护, 避免遇到非 GBK 字符（如零宽空格等）时抛 UnicodeEncodeError
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 免费版会员引导 QQ（小鲸鱼）
FREE_QQ = "3808239548"

# ---------------- 排除清单 ----------------
# 目录级排除（相对主仓根）:
#   以 "/" 结尾 = 根级目录前缀（只排顶层, 不伤子目录同名）
#   带斜杠     = POSIX 前缀（相对主仓根）
#   简单名/通配 = 路径段命中或 fnmatch 命中即排除
EXCLUDE_DIRS = [
    "server/",           # 顶层授权服务端（worker.js 等; src/server 是 FastAPI 本体, 保留）
    "src/tasks/",        # 日常任务执行器（付费, 执行器整体不入树）
    "src/gui/studio/",   # 视觉工坊独立前端（开发者）
    "build", "dist",     # 构建产物
    ".git", "__pycache__", ".vscode", ".idea", ".zcode",
    ".venv*", "venv*",   # 虚拟环境 (含 .venv, .venv-build 等)
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "htmlcov",
    "node_modules", ".github",
    "data/",             # 根级运行数据（历史库/截图/抓包缓存）; 注意 src/pvp/data 是竞技数据, 不匹配
    "data/config/",      # 根级运行配置（含用户 settings; ROI 模板由 INCLUDE_FORCE 带入）
    "docs",              # 主仓内部文档（本仓库有自己的 docs）
    "output",            # 对局截图/敌方头像/OCR 失败帧（含用户对局数据）
    "src-tauri/target", "src-tauri/gen",
    "tools",             # 主仓 tools = 构建链/调试脚本/抓包工具(免费仓有自己的 tools)
    "scripts",
    "archive",           # 主仓历史归档(旧 pywebview 壳等, 顶层 import 已删模块)
]

# 文件级排除（相对主仓根的 POSIX 路径或通配符）
# 付费自动化「物理抽壳」: 丢球工具/挂机引擎本体由 STUBS 顶替(属性面兼容),
# 拟人按键层/AI 接管/背包 OCR 整文件不进树 —— 改闸门也变不出执行代码。
EXCLUDE_FILES = [
    # 根目录调试与运行残留
    "*.log",
    "probe_*.log",
    "_exp_battle_events.json",
    "_shiny_exp_events.json",
    "_shiny_pair.json",
    # 授权体系（免费版换群口令验证存根）
    "src/gui/auth.py",
    "src/gui/auth_core.py",
    "src/gui/auth_core.pyd",
    "src/gui/bridge_auth.py",
    # 付费自动化执行层（由存根顶替或直接缺席）
    "auto_throw_ball.py",             # 丢球工具本体(Interception 按键) → STUB
    "src/states/battle_engine.py",    # 挂机引擎(自动战斗) → STUB
    "src/gui/ai_autopilot.py",        # AI 接管控制器（懒导入, 闸门先行）
    "src/driver/human_input.py",      # 拟人按键规划+执行层
    "src/perception/bag_scanner.py",  # 背包 OCR 盘点（付费）
    "src/perception/ball_watcher.py", # 咕噜球槽位监视（丢球辅助）
    # 抓包工具与测试
    "tests/test_capture.py",
    # 构建链与密钥
    "tools/build_hardened.py",
    "tools/gen_worker_keys.js",
    "tools/pvp_mcp_server.py",
    "keys.json",
]

# 强制包含（位于被排除目录内, 但免费功能依赖的文件; 优先于目录排除）
INCLUDE_FORCE = [
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
    # 2) 付费丢球三开关补闸门: toggle_normal/bomber/skill 直调 tool 未走鉴权,
    #    执行层已抽壳(存根), 闸门保证连日志都不出、直接弹会员引导
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
    # 2b) PVP 识别引擎免费(AI 军师): 摘除 pvp_engine_start 的鉴权闸,
    #     换成入群验证闸(防倒卖: 抓包实现不提供给未验证用户)
    #     (锚点以 if self._pvp_running: 后缀区分 engine_start 的同款闸)
    ("src/gui/bridge_pvp.py",
     '        gate = self._auth_gate()\n        if gate:\n            return gate\n        if self._pvp_running:',
     '        # [FREE BUILD] 识别引擎免费但需入群验证(防倒卖硬闸, 前端 pvp 页同步锁)\n'
     '        gate = self._free_gate()\n'
     '        if gate:\n'
     '            return gate\n'
     '        if self._pvp_running:',
     "pvp_engine_start 摘鉴权闸换入群验证闸(免费识别)"),
    # 2b-2) 抓包子系统启动硬闸: 有人在 RPC 层直接调 collector/capture 也要过验证
    ("src/gui/bridge_pvp.py",
     '    def pvp_collector_start(self) -> dict:',
     '    def pvp_collector_start(self) -> dict:\n'
     '        gate = self._free_gate()  # [FREE BUILD] 入群验证闸(防倒卖)\n'
     '        if gate:\n'
     '            return gate',
     "pvp_collector_start 补入群验证闸"),
    # 2b-3) 数据采集手动抓一帧同理
    ("src/gui/bridge_pvp.py",
     '    def pvp_collector_manual(self) -> dict:',
     '    def pvp_collector_manual(self) -> dict:\n'
     '        gate = self._free_gate()  # [FREE BUILD] 入群验证闸(防倒卖)\n'
     '        if gate:\n'
     '            return gate',
     "pvp_collector_manual 补入群验证闸"),
    # 2b-4) pvp.js: engine_start 被闸拒绝时(free_verify_required)弹验证引导而非无声失败
    ("src/gui/web/assets/pvp.js",
     '            const r = await pywebview.api.pvp_engine_start(src);\n'
     '            if (r.success) {\n'
     '                pvpEngineRunning = true;',
     '            const r = await pywebview.api.pvp_engine_start(src);\n'
     '            if (r && r.free_verify_required) {  // [FREE BUILD] 未验证 → 弹入群验证\n'
     '                if (window.freeVerifyFlow) freeVerifyFlow();\n'
     '                addLog(r.message || \'请先完成入群验证\', \'warning\');\n'
     '            } else if (r.success) {\n'
     '                pvpEngineRunning = true;',
     "pvp.js 引擎启动失败弹验证引导"),
    # 2c) MCP 自玩按键注入补闸(pvp_act 无鉴权直通 human_input; human_input 已排除)
    ("src/gui/bridge_pvp_data.py",
     '    def pvp_act(self, action: str, delay: float = None) -> dict:\n        """执行一条 PVP 操作命令',
     '    def pvp_act(self, action: str, delay: float = None) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] MCP 自玩为付费功能\n        if gate:\n            return gate\n        """执行一条 PVP 操作命令',
     "pvp_act 补闸门"),
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
    # 6) 摘除 F8 后启动日志文案同步(避免免费版日志谎称 F8 可用)
    ("src/gui/bridge_widget.py",
     '            print("[热键] keyboard 库注册完成: F8截图/F11急停", flush=True)\n            self._enqueue_log("快捷键: F8截图/F11急停", "info")',
     '            print("[热键] keyboard 库注册完成: F11急停", flush=True)  # [FREE BUILD] F8 属付费\n            self._enqueue_log("快捷键: F11急停", "info")',
     "F8 文案同步"),
    # 7) 背包盘点(抓包计球, 辅助丢球)属付费: bag_open 是真实按键自动化, 必须硬闸
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
    # 8) 锁幕选择器与拆分线对齐: 主仓锁 pvp/aipvp/daily, 免费版 pvp 是噱头必须放开,
    #    只锁 aipvp+daily
    ("src/gui/web/assets/app.js",
     "    document.querySelectorAll('#page-pvp, #page-aipvp, #page-daily').forEach(page => {",
     "    document.querySelectorAll('#page-aipvp, #page-daily').forEach(page => {  // [FREE BUILD] pvp 免费",
     "锁幕选择器对齐拆分线(pvp 摘除)"),
    # 9) 默认落地页 = PVP 对战(免费噱头页), 而非主仓的丢球助手页;
    #    同时改掉「vision → throw」的用户模式回退跳转(vision 已删, 回 pvp)
    ("src/gui/web/index.html",
     '<section class="page active" id="page-auto">',
     '<section class="page" id="page-auto">  <!-- [FREE BUILD] 默认页改 pvp -->',
     "默认 active 页摘除(auto)"),
    ("src/gui/web/index.html",
     '<section class="page" id="page-pvp">',
     '<section class="page active" id="page-pvp">',
     "默认 active 页置为 pvp"),
    ("src/gui/web/index.html",
     '<button class="nav-item active" data-page="auto" onclick="switchPage(\'throw\')">',
     '<button class="nav-item" data-page="auto" onclick="switchPage(\'auto\')">  <!-- [FREE BUILD] alias fix + nav freedom -->',
     "默认导航高亮摘除(auto) + onclick 修复(throw→auto)"),
    ("src/gui/web/index.html",
     '<button class="nav-item" data-page="pvp" onclick="switchPage(\'pvp\')">',
     '<button class="nav-item active" data-page="pvp" onclick="switchPage(\'pvp\')">',
     "默认导航高亮置为 pvp"),
    ("src/gui/web/assets/app.js",
     "            if (document.querySelector('#page-vision.active')) switchPage('throw');",
     "            if (document.querySelector('#page-auto.active')) switchPage('pvp');  // [FREE BUILD]",
     "用户模式回退跳转改 pvp"),
    # 11) 免费版端口独立(17366), 不与付费版(17365)互抢 — Tauri 壳 + server_main 默认端口同步改
    ("server_main.py",
     'default=17365',
     'default=17366',
     "server_main 默认端口 17366"),
    ("src-tauri/src/main.rs",
     '127.0.0.1:17365',
     '127.0.0.1:17366',
     "Tauri 壳端口 17366(两处)"),
    # 12) web_adapter 适配器: Tauri/WebView2 下 location.origin 是 http://tauri.localhost
    #     (也以 http 开头, 主仓的 origin 推断必然选中它) → RPC/WS 全部打到 Tauri 静态
    #     资源服务, 后端 17366 完全收不到 → "启动识别点了没反应"。免费版端口独立,
    #     直接硬编码, 不做任何 origin 推断。
    ("src/gui/web/assets/web_adapter.js",
     "    const API_BASE = window.location.origin.startsWith('http') \n        ? window.location.origin \n        : 'http://127.0.0.1:17365';",
     "    // [FREE BUILD] Tauri/WebView2 的 origin 是 http://tauri.localhost(也以 http 开头),\n"
     "    // 主仓的 origin 推断必然选错; 免费版端口独立(17366), 硬编码后端基址\n"
     "    const API_BASE = 'http://127.0.0.1:17366';",
     "适配器 API_BASE 硬编码 17366"),
    ("src/gui/web/assets/web_adapter.js",
     "        const wsHost = (window.location.protocol.startsWith('http') && window.location.host)\n            ? window.location.host\n            : '127.0.0.1:17365';",
     "        // [FREE BUILD] 同 API_BASE: Tauri 下 location.host 是 tauri.localhost, 硬编码\n        const wsHost = '127.0.0.1:17366';",
     "适配器 WS host 硬编码 17366"),
    # 13) 去抓包化: 数据源固定+UI 隐藏+文案改口(用户不应感知"抓包"实现细节;
    #     OCR 代码整体保留为备胎, 仅 UI 不可达 — 游戏协议变更时可由作者恢复)
    ("src/gui/web/index.html",
     '<select id="pvpSourceSelect" class="form-select" style="width:auto"',
     '<select id="pvpSourceSelect" class="form-select" style="width:auto;display:none"',
     "隐藏数据源选择框"),
    ("src/gui/web/index.html",
     '<div class="splash-title">洛克王国 · PVP 助手</div>',
     '<div class="splash-title">洛克小助手</div>',
     "splash 品牌名"),
    ("src/gui/web/index.html",
     'id="collectorBar" style="margin-bottom:14px"',
     'id="collectorBar" style="margin-bottom:14px;display:none"',
     "隐藏数据采集条(开发者工具)"),
    ("src/gui/bridge_pvp.py",
     '        src = str(source or "").lower()\n        if src not in ("capture", "rkpp", "ocr"):\n            src = getattr(self, "_pvp_source", "") or self._load_pvp_source() or "ocr"',
     '        src = "capture"  # [FREE BUILD] 数据源固定实时同步(OCR 代码保留备胎, UI 已隐藏)',
     "数据源固定 capture"),
    ("src/gui/bridge_pvp.py",
     '"抓包子系统已启动(同构数据源)"',
     '"实时数据通道已启动"',
     "日志改口: 子系统启动"),
    ("src/gui/bridge_pvp.py",
     '"抓包子系统启动失败: {e}"',
     '"实时数据通道启动失败: {e}"',
     "日志改口: 子系统失败"),
    ("src/gui/bridge_pvp.py",
     '{"capture": "抓包", "rkpp": "RKPP 解码"}',
     '{"capture": "实时", "rkpp": "实时"}',
     "日志改口: 数据源标签(x2)"),
    ("src/gui/bridge_pvp.py",
     '"自研抓包源运行中"',
     '"实时数据源运行中"',
     "状态文案改口"),
    ("src/gui/web/roster_bar.html",
     '对战中 · 阵容数据未同步（主控台数据源请选「自研抓包」）',
     '对战中 · 阵容数据未同步',
     "阵容条文案去抓包指引"),
    # 14) 壁纸为付费特权(作者定义的软特权, 不弹窗不加锁): 免费版无入口、不应用壁纸
    ("src/gui/web/index.html",
     '<button class="tbtn" id="tbBtnWallpaper" title="壁纸场景" onclick="toggleWallpaperPanel()">',
     '<button class="tbtn" id="tbBtnWallpaper" title="壁纸场景" onclick="toggleWallpaperPanel()" style="display:none">',
     "隐藏标题栏壁纸按钮"),
    ("src/gui/web/index.html",
     "    function init() {\n        var c = customUrl();",
     "    function init() {\n"
     "        // [FREE BUILD] 壁纸切换为付费特权; 免费版固定默认壁纸(好看但不给换)\n"
     "        _paint(WALLPAPERS[0].url); return;\n"
     "        var c = customUrl();",
     "免费版固定默认壁纸(不可切换)"),
    ("src/gui/web/assets/pvp.js",
     "{ ocr: 'OCR 识别', capture: '自研抓包', rkpp: '自研解码' }",
     "{ ocr: '实时', capture: '实时', rkpp: '实时' }",
     "前端数据源标签改口"),
    # 10) 悬浮窗(battle_hud)隐藏 AI 接管: 按钮与提示文案都指向付费功能
    ("src/gui/web/battle_hud.html",
     '            <button class="btn-autopilot" id="btnAutopilot" onclick="toggleAutopilot()" title="AI 自动接管出招">',
     '            <button class="btn-autopilot" id="btnAutopilot" onclick="toggleAutopilot()" title="AI 自动接管出招" style="display:none">  <!-- [FREE BUILD] AI 接管属付费 -->',
     "悬浮窗隐藏 AI 接管按钮"),
    ("src/gui/web/battle_hud.html",
     "        b.classList.toggle('active', on);\n        b.title = on ? 'AI 自动驾驶运行中 (F11 急停) - 点击停止' : 'AI 自动接管出招';",
     "        b.classList.toggle('active', on);\n        b.style.display = 'none';  // [FREE BUILD] AI 接管属付费, 状态刷新也保持隐藏\n        b.title = on ? 'AI 自动驾驶运行中 (F11 急停) - 点击停止' : 'AI 自动接管出招';",
     "接管按钮状态刷新保持隐藏"),
    ("src/gui/web/battle_hud.html",
     '「AI 接管」可在主控台 PVP 页开启 · 接管后此处同步显示执行状态',
     'AI 军师建议对免费版完整可用',
     "军师空态文案去接管引导(HTML+JS默认值)"),
]

# ---------------- 存根 ----------------
# 免费树中的付费闸门: AuthUpdateMixin 是唯一闸门实现, 其余 Mixin 全量带入真文件。
# _auth_gate 返回 {"auth_required": True} → 前端 FREE_BUILD 补丁据道拦截并弹会员引导。
STUBS = {
    "src/gui/bridge_auth.py": (
        '"""[Free 构建存根] 免费版授权: 付费闸门统一指向会员引导 + 入群验证防倒卖。"""\n\n'
        'import hashlib\n'
        'import json as _json\n'
        'import time as _time\n\n\n'
        f'FREE_QQ = "{FREE_QQ}"  # 会员购买引导(小鲸鱼)\n\n\n'
        'def get_hwid() -> str:\n'
        '    """机器码: MAC + 主机名 + Windows MachineGuid 的 SHA256(与 Pro 版 auth_core 同算法)。\n'
        '    MachineGuid 重装系统才变; 改 MAC / 改主机名 / 虚拟机克隆都不能"换机"。任一因子缺失自动降级。"""\n'
        '    import socket\n'
        '    import uuid\n'
        '    parts = []\n'
        '    try:\n'
        '        parts.append(str(uuid.getnode()))\n'
        '    except Exception:\n'
        '        pass\n'
        '    try:\n'
        '        parts.append(socket.gethostname())\n'
        '    except Exception:\n'
        '        pass\n'
        '    try:\n'
        '        import winreg\n'
        '        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,\n'
        '                            r"SOFTWARE\\Microsoft\\Cryptography", 0,\n'
        '                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:\n'
        '            parts.append(winreg.QueryValueEx(k, "MachineGuid")[0])\n'
        '    except Exception:\n'
        '        pass\n'
        '    if not parts:  # 兜底: 理论不可达\n'
        '        parts = ["lkw-free-fallback"]\n'
        '    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]\n\n\n'
        'def _free_token_file():\n'
        '    from src.gui.bridge_common import CONFIG_DIR\n'
        '    return CONFIG_DIR / "free_auth.json"\n\n\n'
        'def _free_verified(qq: str = "") -> dict | None:\n'
        '    """本地已验证状态(离线放行的唯一依据)。qq 非空时需匹配。"""\n'
        '    try:\n'
        '        st = _json.loads(_free_token_file().read_text(encoding="utf-8"))\n'
        '        if st.get("token") and (not qq or st.get("qq") == qq):\n'
        '            return st\n'
        '    except Exception:\n'
        '        pass\n'
        '    return None\n\n\n'
        'class AuthUpdateMixin:\n'
        '    """Free 构建授权存根: 闸门=会员引导, 账号接口=免费版提示。"""\n\n'
        '    def _auth_gate(self) -> dict | None:\n'
        '        """付费功能硬闸: 全部拒绝并引导开通会员(前端据此弹引导页)。"""\n'
        '        return {"ok": False, "auth_required": True, "free_build": True,\n'
        '                "message": "该功能为付费版功能, 请开通会员", "pro_qq": FREE_QQ}\n\n'
        '    def _free_gate(self) -> dict | None:\n'
        '        """抓包类功能硬闸: 未完成入群验证则拒绝(PVP 识别页前端同步锁)。"""\n'
        '        if _free_verified():\n'
        '            return None\n'
        '        return {"ok": False, "free_verify_required": True, "free_build": True,\n'
        '                "message": "请先完成入群验证(QQ号+群口令), 验证免费"}\n\n'
        '    def free_verify_status(self) -> dict:\n'
        '        """前端启动时轮询: 是否已通过入群验证。"""\n'
        '        st = _free_verified()\n'
        '        return {"ok": True, "free_build": True, "verified": bool(st),\n'
        '                "qq": (st or {}).get("qq", ""),\n'
        '                "nickname": (st or {}).get("nickname", ""),\n'
        '                "hwid": get_hwid()}\n\n'
        '    def auth_status(self) -> dict:\n'
        '        verified = bool(_free_verified())\n'
        '        return {"ok": True, "authorized": False, "dev_mode": False,\n'
        '                "free_build": True, "pro_qq": FREE_QQ, "free_verified": verified,\n'
        '                "nickname": ("免费版" if not verified else\n'
        '                             "训练家_" + (_free_verified().get("qq", "")[-4:])),\n'
        '                "expires_at": 0}\n\n'
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
        '        hwid 一律由后端生成(前端传入值不信任), token 落地 data/config/free_auth.json\n'
        '        后离线放行; 机器码绑定防一码多机。"""\n'
        '        import urllib.request as _ur\n'
        '        token_file = _free_token_file()\n'
        '        st = _free_verified(qq=str(qq or ""))\n'
        '        if st:\n'
        '            return {"ok": True, "verified": True, "nickname": st.get("nickname", ""),\n'
        '                    "message": "本机已验证"}\n'
        '        qq = str(qq or "").strip()\n'
        '        group_key = str(group_key or "").strip()\n'
        '        if not (qq and group_key):\n'
        '            return {"ok": False, "verified": False, "message": "请填写 QQ 号和群口令"}\n'
        '        try:\n'
        '            req = _ur.Request(\n'
        '                "https://lucky-cell-cd0b.zzx051012-e82.workers.dev/free/verify",\n'
        '                data=_json.dumps({"qq": qq, "group_key": group_key,\n'
        '                                  "hwid": get_hwid()}).encode(),\n'
        '                headers={"Content-Type": "application/json",\n'
        '                         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})\n'
        '            with _ur.urlopen(req, timeout=15) as resp:\n'
        '                data = _json.loads(resp.read().decode("utf-8"))\n'
        '            if not data.get("ok"):\n'
        '                return {"ok": False, "verified": False, "message": data.get("message", "验证失败")}\n'
        '            token_file.parent.mkdir(parents=True, exist_ok=True)\n'
        '            token_file.write_text(_json.dumps(\n'
        '                {"qq": qq, "token": data.get("token", ""),\n'
        '                 "nickname": data.get("nickname", ""),\n'
        '                 "verified_at": _time.time()},\n'
        '                ensure_ascii=False), encoding="utf-8")\n'
        '            try:\n'
        '                self._enqueue_log("入群验证通过, 抓包识别功能已解锁", "success")\n'
        '            except Exception:\n'
        '                pass\n'
        '            return {"ok": True, "verified": True, "nickname": data.get("nickname", ""),\n'
        '                    "message": "验证通过, 感谢支持正版"}\n'
        '        except Exception as e:\n'
        '            return {"ok": False, "verified": False, "message": f"验证服务不可达: {e}"}\n'
    ),
    # ---- 付费自动化「物理抽壳」存根: 属性面兼容桥接层, 执行层不入树 ----
    "auto_throw_ball.py": (
        '"""[Free 构建存根] 丢球/轰炸/技能为 Pro 功能: Interception 执行层不入免费树。\n'
        '保留窗口查询(浮窗定位)与状态属性(状态栏轮询), 动作方法一律拒绝。"""\n'
        'import ctypes\n\n'
        'user32 = ctypes.windll.user32\n\n\n'
        'class AutoThrowBall:\n'
        '    TARGET_WINDOW_TITLE = "洛克王国：世界"\n'
        '    TARGET_WINDOW_CLASS = "UnrealWindow"\n\n'
        '    def __init__(self, on_log=None, **_):\n'
        '        self._on_log = on_log or (lambda *a, **k: None)\n'
        '        self._frame_provider = None\n'
        '        self.conflict_hook = None\n'
        '        self.running = False\n'
        '        self.bomber_running = False\n'
        '        self.skill_running = False\n'
        '        self.normal_count = 0\n'
        '        self.bomber_count = 0\n'
        '        self.skill_count = 0\n'
        '        self.current_state = "stopped"\n'
        '        self.exit_on_battle = False\n'
        '        self.stop_after_count = 0\n'
        '        self.stop_after_minutes = 0\n'
        '        self.selected_ball_id = None\n'
        '        self._run_started_at = None\n'
        '        # CONFIG_SCHEMA 延时键(_get_throw_config 逐键 getattr, 缺属性即崩)\n'
        '        self.normal_min = 0.5\n'
        '        self.normal_max = 0.8\n'
        '        self.bomber_charge_min = 0.3\n'
        '        self.bomber_charge_max = 0.5\n'
        '        self.bomber_hover_min = 2.0\n'
        '        self.bomber_hover_max = 2.2\n'
        '        self.skill_min = 1.0\n'
        '        self.skill_max = 2.0\n\n'
        '    def _log(self, msg, level="info"):\n'
        '        self._on_log(str(msg), level)\n\n'
        '    def _pro(self):\n'
        '        self._log("该功能为付费版功能, 请开通会员", "warning")\n\n'
        '    # ---- 动作方法: RPC 层已闸, 此处兜底拒绝 ----\n'
        '    def toggle(self):\n'
        '        self._pro()\n'
        '        return False\n\n'
        '    def toggle_bomber(self):\n'
        '        self._pro()\n'
        '        return False\n\n'
        '    def toggle_skill(self):\n'
        '        self._pro()\n'
        '        return False\n\n'
        '    def start_normal(self):\n'
        '        self._pro()\n'
        '        return False\n\n'
        '    def stop_all(self):\n'
        '        self.running = self.bomber_running = self.skill_running = False\n'
        '        self.current_state = "stopped"\n\n'
        '    def register_hotkeys(self):\n'
        '        self._log("丢球热键为 Pro 功能, 未注册", "info")\n\n'
        '    def attach_ball_watcher(self, watcher):\n'
        '        return {"success": False, "message": "咕噜球监视为 Pro 功能"}\n\n'
        '    def update_config(self, params):\n'
        '        for k, v in (params or {}).items():\n'
        '            if hasattr(self, k):\n'
        '                setattr(self, k, v)\n'
        '        return {"success": True}\n\n'
        '    # ---- 窗口查询: 浮窗定位需要, 真实现(纯查询, 无付费逻辑) ----\n'
        '    def get_game_hwnd(self) -> int:\n'
        '        hwnd = user32.GetForegroundWindow()\n'
        '        if hwnd:\n'
        '            length = user32.GetWindowTextLengthW(hwnd)\n'
        '            if length > 0:\n'
        '                buf = ctypes.create_unicode_buffer(length + 1)\n'
        '                user32.GetWindowTextW(hwnd, buf, length + 1)\n'
        '                if self.TARGET_WINDOW_TITLE in buf.value:\n'
        '                    return hwnd\n'
        '        hwnd = user32.FindWindowW(self.TARGET_WINDOW_CLASS, None)\n'
        '        if hwnd:\n'
        '            return hwnd\n'
        '        return user32.FindWindowW(None, self.TARGET_WINDOW_TITLE) or 0\n\n'
        '    def is_game_window_active(self) -> bool:\n'
        '        hwnd = user32.GetForegroundWindow()\n'
        '        if not hwnd:\n'
        '            return False\n'
        '        length = user32.GetWindowTextLengthW(hwnd)\n'
        '        if length <= 0:\n'
        '            return False\n'
        '        buf = ctypes.create_unicode_buffer(length + 1)\n'
        '        user32.GetWindowTextW(hwnd, buf, length + 1)\n'
        '        return self.TARGET_WINDOW_TITLE in buf.value\n'
    ),
    "src/states/battle_engine.py": (
        '"""[Free 构建存根] 挂机引擎(自动战斗)为 Pro 功能: 引擎本体不入免费树。"""\n\n\n'
        'class BattleEngine:\n'
        '    def __init__(self, frame_provider=None, on_log=None, dry_run=False):\n'
        '        self._frame_provider = frame_provider\n'
        '        self._on_log = on_log or (lambda *a, **k: None)\n'
        '        self.running = False\n'
        '        self.dry_run = dry_run\n'
        '        self.state = "stopped"\n'
        '        self.state_detail = ""\n'
        '        self._stop_all_cb = None\n'
        '        self.battles_done = 0\n'
        '        self.catch_attempts = 0\n'
        '        self.shiny_count = 0\n'
        '        self.balls_used_total = 0\n'
        '        self.catches = 0\n\n'
        '    def start(self, overrides=None):\n'
        '        self._on_log("挂机引擎为付费版功能, 请开通会员", "warning")\n'
        '        return False\n\n'
        '    def stop(self, reason="手动停止"):\n'
        '        self.running = False\n'
        '        self.state = "stopped"\n\n'
        '    def get_status(self):\n'
        '        return {"running": self.running, "dry_run": self.dry_run,\n'
        '                "state": self.state, "detail": self.state_detail,\n'
        '                "battles_done": self.battles_done, "catch_attempts": self.catch_attempts,\n'
        '                "shiny_count": self.shiny_count, "balls_used_total": self.balls_used_total,\n'
        '                "catches": self.catches, "skills_used": 0, "catch_hp": 0,\n'
        '                "enemy_name": "", "enemy_hp": 0, "shiny_alert": None}\n'
    ),
    # 免费树 src/pvp 用主仓真 __init__(re-export 的模块全部随 src/pvp 整体带入), 无需存根
}

# ---------------- 免费专属模块注入 ----------------
# 免费版独立增值功能(主仓没有): 文件从 tools/free_inject/ 原样拷入免费树,
# 接线补丁在 FREE_INJECT_PATCHES。远行商人 = 免费专属(2026-10-01)。
FREE_INJECT_ROOT = Path(__file__).resolve().parent / "free_inject"

# 注入文件的树内落点(相对主仓根): free_inject 下的相对路径 = 免费树相对路径
INJECT_FILES = [
    "src/gui/bridge_merchant.py",
    "src/gui/bridge_clicker.py",
    "src/gui/web/assets/merchant.js",
    "src/gui/web/assets/merchant.css",
]

# 免费专属模块的接线补丁: 语义同 SOURCE_PATCHES, 但锚点失效时按"已注入"幂等跳过
# 2026-10-01 工具箱页签化后, 前端接线(index.html 页签/app.js 路由)已收进主仓原生代码,
# 不再需要注入补丁; bridge.py 三处保留(幂等, 主仓文件可能被还原后重接)。
# 2026-10-02 新增鼠标连点器(bridge_clicker.py, 免费专属): 纯 SendInput 鼠标事件,
# 与主仓付费自动化执行层(human_input/battle_engine)无任何代码关联。
FREE_INJECT_PATCHES = [
    # 1) AppBridge 挂 Mixin — 2026-10-02 起主仓已原生带 MerchantMixin(工具箱页签化同步),
    #    免费版只需补 ClickerMixin; 锚点按"缺什么补什么"幂等处理:
    #    - ClickerMixin import 缺失 → 补 import 行
    #    - 类基类列表无 ClickerMixin → 在 MerchantMixin 行后补挂
    #    (主仓若回退到无 Merchant 状态, 旧锚点逻辑由下方 2/3 号补丁兜底)
    ("src/gui/bridge.py",
     "from src.gui.bridge_tools import ToolsMixin",
     "from src.gui.bridge_tools import ToolsMixin\nfrom src.gui.bridge_clicker import ClickerService as ClickerMixin  # [CLICKER]",
     "import ClickerMixin(主仓原生 Merchant 时)"),
    ("src/gui/bridge.py",
     "    MerchantMixin,  # [MERCHANT] 远行商人(免费专属)\n):",
     "    MerchantMixin,  # [MERCHANT] 远行商人(免费专属)\n    ClickerMixin,   # [CLICKER] 鼠标连点器(免费专属)\n):",
     "AppBridge 挂 ClickerMixin(主仓原生 Merchant 时)"),
    ("src/gui/bridge.py",
     "class AppBridge(\n    WidgetMixin, AuthUpdateMixin, DailyMixin, RuntimeMixin, GameMixin,\n    VisionMixin, PvpEngineMixin, PvpDataMixin, SettingsMixin, ToolsMixin,\n):",
     "class AppBridge(\n    WidgetMixin, AuthUpdateMixin, DailyMixin, RuntimeMixin, GameMixin,\n    VisionMixin, PvpEngineMixin, PvpDataMixin, SettingsMixin, ToolsMixin,\n    MerchantMixin,  # [MERCHANT] 远行商人(免费专属)\n    ClickerMixin,   # [CLICKER] 鼠标连点器(免费专属)\n):",
     "AppBridge 挂 MerchantMixin + ClickerMixin(主仓无 Merchant 回退锚点)"),
    ("src/gui/bridge.py",
     "from src.gui.bridge_tools import ToolsMixin",
     "from src.gui.bridge_tools import ToolsMixin\nfrom src.gui.bridge_merchant import MerchantService as MerchantMixin  # [MERCHANT]\nfrom src.gui.bridge_clicker import ClickerService as ClickerMixin  # [CLICKER]",
     "import MerchantMixin + ClickerMixin(主仓无 Merchant 回退锚点)"),
    ("src/gui/bridge.py",
     "        self._load_throw_config()",
     "        self._load_throw_config()\n        # [MERCHANT] 远行商人服务状态初始化(纯缓存字段, 无副作用)\n        try:\n            self.merchant_init()\n        except Exception:\n            pass\n        # [CLICKER] 连点器服务(免费专属): 热键注册失败不影响主功能\n        try:\n            from src.gui.bridge_clicker import ClickerService\n            self.clicker = ClickerService(log=self._enqueue_log)\n            self.clicker.install_hotkeys()\n        except Exception as _e:\n            self.clicker = None\n            self._enqueue_log(f'连点器初始化失败: {_e}', 'warning')",
     "AppBridge.__init__ 调 merchant_init + 连点器初始化"),
]

# ---------------- 免费版前端补丁 ----------------
# index.html: <title> 定名 + </body> 前 FREE_BUILD 脚本(付费页拦截 + 会员引导弹窗)
# app.js: NAV_PAID_PAGES 从 ['pvp','aipvp','daily'] 改为 ['auto','aipvp','daily']
#         (免费噱头 = pvp 页; 付费 = 丢球/挂机页 + AI 对战接管 + 日常)
FREE_PATCH_HTML = r"""<script>
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
    // MERGED_PAGE_MAP 是 app.js 内部 const, 不在 window 上, 这里硬编码别名:
    // throw/engine → auto (丢球助手), mcp/aivision/aibuddy → aipvp (AI对战)
    if (typeof window.switchPage === 'function') {
        var _sw = window.switchPage;
        window.switchPage = function (name) {
            var n = name;
            if (n === 'throw' || n === 'engine') n = 'auto';
            if (n === 'mcp' || n === 'aivision' || n === 'aibuddy') n = 'aipvp';
            if (PAID_PAGES[n]) { showProModal(PAID_PAGES[n]); return; }
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
            // DOM 级 onclick 覆写: 把 label 存 data 属性绕过引号问题
            b.setAttribute('data-pro-label', PAID_PAGES[p]);
            b.setAttribute('onclick', "var l=this.getAttribute('data-pro-label');window.showProModal&&showProModal(l)");
            nav.appendChild(b);   // 沉底
        });
        // 隐藏已删除页面的导航残留(理论上下游 CSS 已无, 兜底)
        // 注: tools(工具箱)不在删除清单 — 图鉴/远行商人收进工具箱页签后它是免费功能载体
        ['vision', 'bag', 'config'].forEach(function (p) {
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
    // WebView2 二次锁: pywebviewready 链路可能在 DOMContentLoaded 之后复位导航,
    // 延迟一口确保 onclick 覆写不被任何异步初始化覆盖
    setTimeout(freeNav, 600);
    // 头像点击: 已验证提示, 未验证弹入群验证(防倒卖: 抓包识别功能须验证后解锁)
    window.authClick = function () {
        freeVerifyFlow();
    };
    // ---- 入群验证弹层(QQ号 + 群口令双输入, customModal 单框不敷用, 自建轻量层) ----
    var _freeVerifyOverlay = null;
    function freeVerifyEnsureOverlay() {
        if (_freeVerifyOverlay) return _freeVerifyOverlay;
        var ov = document.createElement('div');
        ov.className = 'custom-modal-overlay';
        ov.id = 'freeVerifyOverlay';
        ov.innerHTML = '<div class="custom-modal" style="max-width:340px">'
            + '<h3>入群验证</h3>'
            + '<p style="font-size:12px;opacity:.75;line-height:1.7">PVP 实时识别需要完成免费的入群验证。<br>'
            + 'QQ 号用于绑定本机(一号一机), 群口令见 QQ 群公告。</p>'
            + '<div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">'
            + '<input type="text" id="freeVfQq" class="form-input" placeholder="QQ 号" maxlength="12" autocomplete="off">'
            + '<input type="text" id="freeVfKey" class="form-input" placeholder="QQ 群口令(见群公告)" autocomplete="off">'
            + '</div>'
            + '<div class="custom-modal-actions" style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">'
            + '<button class="btn btn-ghost" id="freeVfCancel">取消</button>'
            + '<button class="btn btn-primary" id="freeVfGo">验证</button>'
            + '</div></div>';
        document.body.appendChild(ov);
        _freeVerifyOverlay = ov;
        ov.querySelector('#freeVfCancel').onclick = function () { ov.classList.remove('show'); };
        ov.onclick = function (e) { if (e.target === ov) ov.classList.remove('show'); };
        return ov;
    }
    async function freeVerifyFlow() {
        var ov = freeVerifyEnsureOverlay();
        var qqEl = ov.querySelector('#freeVfQq'), keyEl = ov.querySelector('#freeVfKey');
        qqEl.value = ''; keyEl.value = '';
        ov.classList.add('show');
        setTimeout(function () { qqEl.focus(); }, 60);
        var goBtn = ov.querySelector('#freeVfGo');
        goBtn.onclick = async function () {
            var qq = qqEl.value.trim(), key = keyEl.value.trim();
            if (!/^\d{5,12}$/.test(qq)) { if (window.showToast) showToast('请填写正确的 QQ 号', 'warning'); return; }
            if (!key) { if (window.showToast) showToast('请填写群口令(见 QQ 群公告)', 'warning'); return; }
            goBtn.disabled = true; goBtn.textContent = '验证中…';
            try {
                var r = await pywebview.api.free_verify_group(qq, key);
                if (r && r.ok && r.verified) {
                    ov.classList.remove('show');
                    if (window.showToast) showToast(r.message || '验证通过', 'success');
                    window.FREE_VERIFIED = true;
                    if (window.freeApplyVerifyUI) freeApplyVerifyUI(true);
                } else {
                    if (window.showToast) showToast((r && r.message) || '验证失败', 'error');
                }
            } catch (e) {
                if (window.showToast) showToast('验证异常: ' + e, 'error');
            }
            goBtn.disabled = false; goBtn.textContent = '验证';
        };
        keyEl.onkeydown = function (e) { if (e.key === 'Enter') goBtn.click(); };
        qqEl.onkeydown = function (e) { if (e.key === 'Enter') keyEl.focus(); };
    }
    window.freeVerifyFlow = freeVerifyFlow;
    // ---- PVP 页锁幕: 未验证盖"入群验证"引导(后端 _free_gate 同步硬闸) ----
    window.freeApplyVerifyUI = function (verified) {
        var page = document.getElementById('page-pvp');
        if (!page) return;
        var veil = page.querySelector('.free-verify-veil');
        if (verified) {
            if (veil) veil.remove();
            var sub = document.getElementById('authState');
            if (sub) sub.innerHTML = '<span class="auth-exp">已验证 · 感谢支持正版</span>';
            return;
        }
        if (veil) return;
        veil = document.createElement('div');
        veil.className = 'auth-veil free-verify-veil';
        veil.innerHTML = '<div class="av-box">'
            + '<div class="av-icon">' + (window.icon ? icon('lock', 36) : '') + '</div>'
            + '<div class="av-title">PVP 识别需要入群验证</div>'
            + '<div class="av-desc">验证完全免费: 输入 QQ 号 + 群口令即可<br>口令见 QQ 群公告 · 一号绑定一机 · 本机数据不上传</div>'
            + '<button class="btn btn-primary" onclick="freeVerifyFlow()">立即验证</button></div>';
        page.appendChild(veil);
    };
    // 启动时查验证态: 未验证 → pvp 页上锁 + 头像提示"点击验证"
    (function freeVerifyBoot() {
        var ask = function () {
            try {
                pywebview.api.free_verify_status().then(function (st) {
                    var ok = !!(st && st.verified);
                    window.FREE_VERIFIED = ok;
                    if (window.freeApplyVerifyUI) freeApplyVerifyUI(ok);
                    if (!ok) {
                        var sub = document.getElementById('authState');
                        if (sub) sub.innerHTML = '<span class="auth-exp">未验证 · 点击验证</span>';
                        var box = document.getElementById('authAvatarBox');
                        if (box) box.title = '点击完成入群验证(免费)';
                    }
                }).catch(function () {});
            } catch (e) { /* web 模式无 pywebview 桩时静默 */ }
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(ask, 400); });
        else setTimeout(ask, 400);
        // pywebview 桩就绪晚于 DOM 的场景再补一口
        window.addEventListener('pywebviewready', function () { setTimeout(ask, 200); });
    })();
    // 免费版身份显示: 已验证显示昵称, 未验证引导验证(覆盖主仓 renderAuth 的未登录文案)
    if (typeof window.renderAuth === 'function') {
        var _ra = window.renderAuth;
        window.renderAuth = function () {
            var r = _ra.apply(this, arguments);
            try {
                var sub = document.getElementById('authState');
                var box = document.getElementById('authAvatarBox');
                if (window.FREE_VERIFIED) {
                    if (sub) sub.innerHTML = '<span class="auth-exp">已验证 · 感谢支持正版</span>';
                    if (box) box.title = '已通过入群验证';
                } else {
                    if (sub) sub.innerHTML = '<span class="auth-exp">未验证 · 点击验证</span>';
                    if (box) box.title = '点击完成入群验证(免费)';
                }
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


FREE_DIST_README = """# 洛克小助手 · Free（桌面端）

洛克王国桌面辅助工具 · 免费版。Tauri 2 (Rust) 桌面端 + FastAPI 后端解耦架构。

## 免费版功能清单

- ⚔️ **PVP 实时识别 & AI 军师**：实时推演与战况雷达 HUD 浮窗、双方阵容顶栏、属性克制即时计算、本机战报。
- 🤖 **AI 智能助手**：支持填入自定义 API Key（DeepSeek/智谱/OpenAI 等），密钥仅保存在本机。
- 🛒 **远行商人**：每日四时段货单聚合速查与倒计时、本机货单历史（免费版专属）。
- 🧰 **实用小工具箱**：
  - 精灵图鉴（全量 685+ 精灵 Wiki 资料与技能速查）
  - 属性克制计算器（18 系双克/双抗多倍率计算）
  - 蛋组查询（孵蛋配种互查）
  - 异色概率计算器（7 大渠道官方概率估算）
  - 异色摆窝规划器（背包库存导入与最优配对摆放求解）

> 💡 **提示**：丢球助手、挂机引擎、日常任务一键托管、AI 对战自动驾驶接管为 Pro 会员版专属功能。
> 免费版中自动化执行层物理抽壳，不包含按键驱动与敏感自动化代码。

## 启动方式

- **桌面客户端（推荐）**：
  直接双击运行 `src-tauri/target/debug/lkwg-pvp-assistant.exe`（自动拉起后台服务）。
- **纯 Python 服务模式**：
  ```bash
  python server_main.py
  ```
  控制台服务默认运行在：`http://127.0.0.1:17366`

## 版本更新与支持

免费版更新请加入 QQ 群获取（群主 QQ: FREE_QQ_PLACEHOLDER）。
"""


def _excluded(rel: Path) -> bool:
    """排除判定: 简单名按路径段匹配, 带斜杠按 POSIX 前缀, 根级(尾/)按顶层前缀, 支持通配符"""
    posix = rel.as_posix()
    for d in EXCLUDE_DIRS:
        if d.endswith("/"):
            if posix.startswith(d):
                return True
        elif "/" in d:
            if posix == d or posix.startswith(d + "/"):
                return True
        elif any(c in d for c in "*?["):
            for part in rel.parts:
                if fnmatch.fnmatch(part, d):
                    return True
        elif d in rel.parts:
            return True
    for f in EXCLUDE_FILES:
        if any(c in f for c in "*?["):
            if fnmatch.fnmatch(posix, f) or fnmatch.fnmatch(rel.name, f):
                return True
        elif posix == f:
            return True
    return False


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
    """对产出的免费树打源码补丁; 命中次数必须 ≥1(允许同锚点多处, 逐处替换),
    零命中时检查替换文本是否已存在(幂等补丁), 仍未命中才报错。"""
    for rel, old, new, why in SOURCE_PATCHES:
        dst = out_root / rel
        if not dst.exists():
            raise SystemExit(f"[sync_free] 补丁目标不存在({why}): {rel}")
        text = dst.read_text(encoding="utf-8")
        n = text.count(old)
        if n < 1:
            if new in text:
                print(f"[sync_free] 补丁跳过(已应用): {why} ({rel})")
                continue
            raise SystemExit(f"[sync_free] 补丁锚点失效({why}): {rel} 命中 0 次, 请核对主仓改动")
        dst.write_text(text.replace(old, new), encoding="utf-8")
        print(f"[sync_free] 补丁 OK: {why} ({rel}, x{n})")


def apply_inject_files(out_root: Path) -> None:
    """把 tools/free_inject/ 下的免费专属文件拷入免费树。

    商人文件(bridge_merchant.py/merchant.js/css)接口保护不入库: 开源 clone 出来的
    仓库天然缺这几个文件, 此处降级为跳过并告警(而不是 SystemExit 中断整树同步);
    本地开发机实体文件存在时行为不变。"""
    merchant_rels = {r for r in INJECT_FILES if "merchant" in r}
    for rel in INJECT_FILES:
        src = FREE_INJECT_ROOT / rel
        dst = out_root / rel
        if not src.exists():
            if rel in merchant_rels:
                print(f"[sync_free] 注入跳过(接口保护, 本地无实体文件): {rel}")
                continue
            raise SystemExit(f"[sync_free] 注入源文件缺失: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"[sync_free] 注入 OK: {rel}")


def apply_inject_patches(out_root: Path) -> None:
    """免费专属模块的接线补丁(幂等同 SOURCE_PATCHES, 作用于注入文件拷入之后)。

    商人相关补丁锚点失效/目标缺失时降级为跳过(与 apply_inject_files 的接口保护
    容错对齐): 开源 clone 树没有商人文件, bridge.py 里的 Mixin 挂载补丁应自然
    跳过; 但若连点器等非商人补丁锚点失效仍硬报错。"""
    for rel, old, new, why in FREE_INJECT_PATCHES:
        is_merchant = "MERCHANT" in why or "merchant" in why.lower()
        dst = out_root / rel
        if not dst.exists():
            if is_merchant:
                print(f"[sync_free] 注入补丁跳过(接口保护): {why} ({rel})")
                continue
            raise SystemExit(f"[sync_free] 注入补丁目标不存在({why}): {rel}")
        text = dst.read_text(encoding="utf-8")
        n = text.count(old)
        if n < 1:
            if new in text:
                print(f"[sync_free] 注入补丁跳过(已应用): {why} ({rel})")
                continue
            if is_merchant:
                print(f"[sync_free] 注入补丁跳过(接口保护, 锚点失效): {why} ({rel})")
                continue
            raise SystemExit(f"[sync_free] 注入补丁锚点失效({why}): {rel} 命中 0 次, 请核对主仓改动")
        dst.write_text(text.replace(old, new), encoding="utf-8")
        print(f"[sync_free] 注入补丁 OK: {why} ({rel}, x{n})")


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
    all_items = copied + [Path(r) for r in stubs] + [Path(r) for r in INJECT_FILES]

    print(f"[sync_free] 计划: 拷贝 {len(copied)} 个文件 + {len(stubs)} 个存根 + {len(INJECT_FILES)} 个注入 + {len(SOURCE_PATCHES)} 个补丁")
    if args.dry_run:
        for rel in sorted(all_items, key=lambda p: p.as_posix()):
            r = rel.as_posix()
            tag = "STUB" if r in stubs else ("INJECT" if r in INJECT_FILES else "COPY")
            print(f"  {tag} {r}")
        return

    if out_root.exists():
        # 保留 Tauri 编译缓存: target 目录移出 → 重建树 → 移回,
        # 之后改前端只影响 HTML/JS, cargo 增量编译秒级完成
        target_dir = out_root / "src-tauri" / "target"
        keep_dir = out_root.parent / (out_root.name + "_target_keep")
        if target_dir.exists():
            shutil.rmtree(keep_dir, ignore_errors=True)
            shutil.move(str(target_dir), str(keep_dir))
        shutil.rmtree(out_root, ignore_errors=True)
    for rel, src in iter_source_files(main_root):
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel, content in stubs.items():
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    # 免费专属模块: 文件注入 + 接线补丁(必须先于 SOURCE_PATCHES 前端补丁? 否,
    # 两者锚点互不重叠, 先注入后接线即可)
    apply_inject_files(out_root)
    apply_inject_patches(out_root)

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

    # 归还 Tauri 编译缓存(见 main() 开头的移出逻辑)
    keep_dir = out_root.parent / (out_root.name + "_target_keep")
    if keep_dir.exists():
        (out_root / "src-tauri").mkdir(parents=True, exist_ok=True)
        shutil.move(str(keep_dir), str(out_root / "src-tauri" / "target"))
        print("[sync_free] Tauri 编译缓存已保留(增量编译)")

    marker = out_root / "FREE_BUILD"
    marker.write_text(
        "洛克小助手 Free 构建产物 — 更新通道: QQ 群获取\n",
        encoding="utf-8")
    readme_free = out_root / "README.md"
    readme_free.write_text(FREE_DIST_README.replace("FREE_QQ_PLACEHOLDER", FREE_QQ), encoding="utf-8")
    print(f"[sync_free] 免费发布说明已写入: {readme_free}")

    # 将本仓库规范的免费版 AGENTS.md 覆盖到产物树, 避免主仓旧规则产生认知冲突
    free_agents = Path(__file__).resolve().parent.parent / "AGENTS.md"
    if free_agents.exists():
        shutil.copy2(free_agents, out_root / "AGENTS.md")
        print("[sync_free] 免费版 AGENTS.md 已同步至产物树")

    print(f"[sync_free] 免费树已生成: {out_root}")
    print("[sync_free] TODO: compileall 冒烟 + check_imports 复查 + 启动验证通过后才可分发")


if __name__ == "__main__":
    main()
