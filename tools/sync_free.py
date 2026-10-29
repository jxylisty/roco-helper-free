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
import os
import shutil
import sys
import time
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
# 入群验证有效期(天): 写入免费树 bridge_auth.py 存根的常量, 前端文案同步引用
FREE_VERIFY_TTL_DAYS = 30

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
    "tools/diag_capture.py",             # 抓包链路 10 秒快检(用户自助排障)
]

# 强制包含的目录: 递归带入整目录（INCLUDE_FORCE 中以目录形式列出的条目）
INCLUDE_FORCE_DIRS = [e for e in INCLUDE_FORCE if not e.endswith(".py")]

# ---------------- 主仓源文件补丁（正则原文替换, 逐条可回溯） ----------------
# 每条: (文件, 旧文本, 新文本, 说明)。命中失败会报错退出, 防止静默漂移。
SOURCE_PATCHES = [
    # 1) 免费版强制用户模式: DEV 页(视觉调试台)与开发者功能全隐藏
    #    (主仓逻辑 2026-10-24 起反转为默认安全: dev_mode.marker 不存在即用户版,
    #     免费树本就无 marker; 此补丁仍显式钉死, 双保险防 marker 误入免费树)
    ("src/gui/bridge_common.py",
     '_DEV_MARKER = PROJECT_ROOT / "data" / "config" / "dev_mode.marker"\nDEV_MODE = _DEV_MARKER.exists() and os.environ.get("LKW_DEV_MODE") != "0"',
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
    # 2026-10-10 同步主仓签名扩展(wait_echo/sync 参数), 锚点跟随更新
    # 2026-10-11 主仓 bridge_pvp_data 拆 7 组: pvp_act 随出招操作组迁 bridge_pvp_act.py
    ("src/gui/bridge_pvp_act.py",
     '    def pvp_act(self, action: str, delay: float = None, wait_echo: bool = False,\n'
     '                sync: bool = False) -> dict:\n'
     '        """执行一条 PVP 操作命令',
     '    def pvp_act(self, action: str, delay: float = None, wait_echo: bool = False,\n'
     '                sync: bool = False) -> dict:\n'
     '        gate = self._auth_gate()  # [FREE BUILD] MCP 自玩为付费功能\n'
     '        if gate:\n'
     '            return gate\n'
     '        """执行一条 PVP 操作命令',
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
    # 6) 摘除 F8 后启动日志文案同步(避免免费版日志谎称 F8 可用);
    #    主仓 2026-10 新增 Ctrl+F2 穿透热键(付费挂机链), 一并摘除
    ("src/gui/bridge_widget.py",
     '            keyboard.add_hotkey(\'f11\', self._emergency_stop)\n'
     '            keyboard.add_hotkey(\'ctrl+f2\', self._hotkey_toggle_click_through)\n'
     '            # stdout 直打: 启动日志可追溯(热键失效时不用猜)\n'
     '            print("[热键] keyboard 库注册完成: F8截图/F11急停/Ctrl+F2穿透开关", flush=True)\n'
     '            self._enqueue_log("快捷键: F8截图/F11急停/Ctrl+F2穿透开关", "info")',
     '            keyboard.add_hotkey(\'f11\', self._emergency_stop)\n'
     '            # stdout 直打: 启动日志可追溯(热键失效时不用猜)\n'
     '            print("[热键] keyboard 库注册完成: F11急停", flush=True)  # [FREE BUILD] F8/Ctrl+F2 属付费\n'
     '            self._enqueue_log("快捷键: F11急停", "info")',
     "F8 文案同步"),
    # 7) 背包盘点(抓包计球, 辅助丢球)属付费: bag_open 是真实按键自动化, 必须硬闸
    # 2026-10-11 主仓 bridge_pvp_data 拆 7 组: 三个 bag_* 随背包组迁 bridge_pvp_bag.py
    ("src/gui/bridge_pvp_bag.py",
     '    def bag_scan(self) -> dict:\n        """背包盘点: 优先协议直读',
     '    def bag_scan(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包盘点为付费功能\n        if gate:\n            return gate\n        """背包盘点: 优先协议直读',
     "bag_scan 补闸门"),
    ("src/gui/bridge_pvp_bag.py",
     '    def bag_open(self) -> dict:\n        """置顶游戏',
     '    def bag_open(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包打开为付费自动化\n        if gate:\n            return gate\n        """置顶游戏',
     "bag_open 补闸门"),
    ("src/gui/bridge_pvp_bag.py",
     '    def bag_open_and_scan(self) -> dict:\n        """打开背包',
     '    def bag_open_and_scan(self) -> dict:\n        gate = self._auth_gate()  # [FREE BUILD] 背包盘点为付费功能\n        if gate:\n            return gate\n        """打开背包',
     "bag_open_and_scan 补闸门"),
    # 8) 锁幕选择器与拆分线对齐: 主仓锁 pvp/aipvp/daily, 免费版 pvp 是噱头必须放开,
    #    只锁 aipvp+daily
    #    2026-10-11 主仓 app.js 六文件拆分: 该锚点随"账户登录区/VIP"落入 app_auth.js
    ("src/gui/web/assets/app_auth.js",
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
    # 2026-10-11 主仓 app.js 六文件拆分: 该锚点随"运行模式"落入 app_auth.js
    ("src/gui/web/assets/app_auth.js",
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
     "    const API_BASE = _isBackendOrigin ? _loc.origin : 'http://127.0.0.1:17365';",
     "    // [FREE BUILD] Tauri/WebView2 的 origin 是 http://tauri.localhost(也以 http 开头),\n"
     "    // 主仓的 origin 推断必然选错; 免费版端口独立(17366), 硬编码后端基址\n"
     "    const API_BASE = 'http://127.0.0.1:17366';",
     "适配器 API_BASE 硬编码 17366"),
    ("src/gui/web/assets/web_adapter.js",
     "        const _wsHostOk = window.location.protocol.startsWith('http') &&\n"
     "            ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);\n"
     "        const wsHost = _wsHostOk ? window.location.host : '127.0.0.1:17365';",
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
     '        src = "rkpp"  # [FREE BUILD] 数据源固定 RKPP 全帧解码(与付费版同链路, 无 OCR);\n'
     '        # 2026-10-04 修正: 此前误接 capture 源 — 那是半成品适配器(精灵名/血量"待补"),\n'
     '        # 表现为对局 vs未知+无战报。rkpp 依赖: 项目同级 rkpp_ref/(rkpp_live_tools.py + Key/latest.key)',
     "数据源固定 rkpp(全帧解码, 与付费版一致)"),
    # 2026-10-10 主仓拆分: 抓包子系统方法搬入 bridge_pvp_source.py(PvpSourceMixin)
    ("src/gui/bridge_pvp_source.py",
     '"抓包子系统已启动(同构数据源)"',
     '"实时数据通道已启动"',
     "日志改口: 子系统启动"),
    ("src/gui/bridge_pvp_source.py",
     '"抓包子系统启动失败: {e}"',
     '"实时数据通道启动失败: {e}"',
     "日志改口: 子系统失败"),
    ("src/gui/bridge_pvp.py",
     '{"capture": "抓包", "rkpp": "RKPP 解码"}',
     '{"capture": "实时数据通道", "rkpp": "极速数据通道"}',
     "日志改口: 数据源标签(x2)"),
    ("src/gui/bridge_pvp.py",
     '"自研抓包源运行中"',
     '"实时数据源运行中"',
     "状态文案改口"),
    ("src/gui/web/roster_bar.html",
     '对战中 · 阵容数据未同步（主控台数据源请选「自研抓包」）',
     '对战中 · 阵容数据未同步',
     "阵容条文案去抓包指引"),  # 锚点已随主仓改口为「极速数据通道」, 仍命中
    # 14) 壁纸为付费特权(作者定义的软特权, 不弹窗不加锁): 免费版无入口、不应用壁纸
    ("src/gui/web/index.html",
     '<button class="tbtn" id="tbBtnWallpaper" title="壁纸场景" onclick="toggleWallpaperPanel()">',
     '<button class="tbtn" id="tbBtnWallpaper" title="壁纸场景" onclick="toggleWallpaperPanel()" style="display:none">',
     "隐藏标题栏壁纸按钮"),
    ("src/gui/web/assets/settings.js",
     "        // 应用壁纸图片并同步两处 UI\n"
     "        var targetWp = custom ? custom : (PRESET_WALLPAPERS[wpIdx] ? PRESET_WALLPAPERS[wpIdx].url : '');\n"
     "        paintWallpaperWithScrim(targetWp, scrim);",
     "        // [FREE BUILD] 壁纸切换为付费特权: 免费版永远钉死默认壁纸(第一张预设),\n"
     "        // 用户本地存的 idx/custom 全部忽略(好看但不给换)\n"
     "        var targetWp = PRESET_WALLPAPERS[0] ? PRESET_WALLPAPERS[0].url : '';\n"
     "        paintWallpaperWithScrim(targetWp, scrim);",
     "免费版固定默认壁纸(不可切换)"),
    # 15) 健康检查「账户状态」探针: 免费树无 auth.py(物理抽壳), 懒导入必抛
    #     cannot import name 'auth' → 用户看到内部报错+误导性"重启助手"。
    #     免费版改读 _free_verified(入群验证态), 语义对齐免费版账户观。
    #     注意: _probe_auth 是 @staticmethod, 原 try 块尾部(auth.status() 之后)
    #     的 DEV_MODE 分支/未登录分支保留但不可达——免费版直接 return。
    ("src/gui/bridge_health.py",
     '    @staticmethod\n'
     '    def _probe_auth() -> dict:\n'
     '        """登录态: 用户版未登录=warn(免费功能可用), 开发者版恒 ok"""\n'
     '        try:\n'
     '            from src.gui import auth\n'
     '            st = auth.status()\n',
     '    @staticmethod\n'
     '    def _probe_auth() -> dict:\n'
     '        """[FREE BUILD] 账户状态: 免费树无 auth 体系, 读入群验证态"""\n'
     '        try:\n'
     '            from src.gui.bridge_auth import _free_verified\n'
     '            if _free_verified():\n'
     '                return HealthMixin._mk("auth", "账户状态", "ok",\n'
     '                                       "已通过入群验证")\n'
     '            return HealthMixin._mk(\n'
     '                "auth", "账户状态", "warn", "未验证",\n'
     '                "点击左侧头像完成入群验证(免费); 验证后解锁 PVP 识别与远行商人")\n'
     '            # ---- 以下为主仓原逻辑, 免费树无 auth 模块, 永不可达 ----\n'
     '            from src.gui import auth\n'
     '            st = auth.status()\n',
     "健康检查账户状态改读入群验证态(免 auth 模块)"),
    # 16) 健康检查「按键驱动」探针: 2026-10-05 连点器已升级 interception 硬件级注入
    #     (防游戏检测封号, 用户明确要求), 驱动重新变成免费版的真实依赖。
    #     探针改写: 驱动在 → ok(硬件级注入); 驱动缺 → warn(非 fail):
    #     连点器自动回退 SendInput 功能不中断, 但反作弊可见性下降, 引导安装。
    ("src/gui/bridge_health.py",
     '    def _probe_interception() -> dict:\n'
     '        """按键驱动: 键盘控制是否可用"""\n'
     '        try:\n'
     '            import interception  # noqa: F401\n'
     '            interception.auto_capture_devices()\n'
     '            return HealthMixin._mk("driver", "按键驱动", "ok",\n'
     '                                   "键盘驱动正常 (自动丢球与挂机功能就绪)")\n'
     '        except Exception as e:\n'
     '            return HealthMixin._mk(\n'
     '                "driver", "按键驱动", "fail", "按键驱动未安装或被拦截",\n'
     '                "自动丢球/挂机需要此驱动: 请重新安装助手并允许驱动安装，"\n'
     '                "或右键以管理员身份运行一次")',
     '    def _probe_interception() -> dict:\n'
     '        """[FREE BUILD] 输入驱动: 连点器硬件级注入(interception)检测。\n'
     '        驱动缺位时连点器自动回退 SendInput(功能可用但防检测弱化)"""\n'
     '        try:\n'
     '            from src.gui.bridge_clicker import interception_available\n'
     '            if interception_available():\n'
     '                return HealthMixin._mk("driver", "输入驱动", "ok",\n'
     '                                       "Interception 驱动正常 (连点器硬件级注入, 防检测)")\n'
     '            raise OSError("驱动未安装")\n'
     '        except Exception:\n'
     '            return HealthMixin._mk(\n'
     '                "driver", "输入驱动", "warn", "Interception 驱动未安装 (连点器已回退兼容模式)",\n'
     '                "连点器功能可用, 但硬件级防检测未生效; 建议安装 Interception 驱动 "\n'
     '                "(github.com/oblitum/Interception) 后重启助手")',
     "健康检查输入驱动改 interception 真实检测(回退 warn 非 fail)"),
    ("src/gui/web/assets/pvp.js",
     "{ ocr: '画面智能识别', capture: '实时数据通道', rkpp: '极速数据通道' }",
     "{ ocr: '实时', capture: '实时', rkpp: '实时' }",
     "前端数据源标签改口"),  # 2026-10-29 主仓统一白话数据源名, 锚点同步更新
    # 10) 悬浮窗(battle_hud)隐藏 AI 接管: 按钮与提示文案都指向付费功能
    #     2026-10-04 适配 3cc180d HUD 电竞级重构(hud-btn class + updateAutopilotBtn)
    #     2026-10-11 适配主仓 battle_hud 拆分(HTML/CSS/JS 三文件):
    # 注: 2026-10-28 主仓已从悬浮窗移除 AI 接管按钮(付费功能统一从主控台操作),
    # 免费版不再需要单独隐藏 —— 同步过来就已经没有了。
    ("src/gui/web/battle_hud.html",
     '「AI 接管」可在主控台 PVP 页开启 · 接管后此处同步显示执行状态',
     'AI 军师建议对免费版完整可用',
     "军师空态文案去接管引导(HTML 默认值)"),
    ("src/gui/web/assets/battle_hud.js",
     '「AI 接管」可在主控台 PVP 页开启 · 接管后此处同步显示执行状态',
     'AI 军师建议对免费版完整可用',
     "军师空态文案去接管引导(JS 渲染值, sigRender)"),
    # 11) toast 置顶: 验证弹窗遮罩(custom-modal-overlay)同为 9999 且 DOM 靠后,
    #     失败 toast 被盖在 80% 不透明毛玻璃下 = 用户眼里"验证失败没提示"
    ("src/gui/web/assets/app.css",
     ".toast-container {\n    position: fixed; top: 16px; right: 16px; z-index: 9999;",
     ".toast-container {\n    position: fixed; top: 16px; right: 16px; z-index: 100000;  /* [FREE BUILD] 置顶: 高于全部弹窗遮罩, 报错提示不被吞 */",
     "toast 层级置顶(修复弹窗内失败提示被遮罩吞掉)"),
]

# ---------------- 存根 ----------------
# 免费树中的付费闸门: AuthUpdateMixin 是唯一闸门实现, 其余 Mixin 全量带入真文件。
# _auth_gate 返回 {"auth_required": True} → 前端 FREE_BUILD 补丁据道拦截并弹会员引导。
STUBS = {
    "src/gui/bridge_auth.py": (
        '"""[Free 构建存根] 免费版授权: 付费闸门统一指向会员引导 + 入群验证防倒卖。"""\n\n'
        'import hashlib\n'
        'import json as _json\n'
        'import time as _time\n\n'
        f'FREE_BUILD_TS = {int(time.time() * 1000)}  # 构建时间戳(ms): Worker 校验构建过期\n'
        f'FREE_BUILD_ID = "{time.strftime("%Y%m%d")}-{os.getpid():x}"  # 分发批次水印: 泄密溯源\n\n\n'
        f'FREE_QQ = "{FREE_QQ}"  # 会员购买引导(小鲸鱼)\n\n'
        '# 入群验证有效期(天): 软过期, 过期后仅抓包识别类功能需重验, 其余功能不受影响\n'
        f'FREE_VERIFY_TTL_DAYS = {FREE_VERIFY_TTL_DAYS}\n\n\n'
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
        '    """本地已验证状态(离线放行的唯一依据)。qq 非空时需匹配。\n'
        '    30 天软过期: verified_at 超 FREE_VERIFY_TTL_DAYS 返回 None(口令在群公告\n'
        '    定期轮换, 提示重验; 期间抓包类功能闸住, 其余功能不受影响)。"""\n'
        '    try:\n'
        '        st = _json.loads(_free_token_file().read_text(encoding="utf-8"))\n'
        '        if st.get("token") and (not qq or st.get("qq") == qq):\n'
        '            if _time.time() - float(st.get("verified_at") or 0) > FREE_VERIFY_TTL_DAYS * 86400:\n'
        '                return None  # [FREE] 验证过期 → 视为待重验\n'
        '            return st\n'
        '    except Exception:\n'
        '        pass\n'
        '    return None\n\n\n'
        'def _free_verified_raw(qq: str = "") -> dict | None:\n'
        '    """不检查过期的原始验证态(供前端判断"曾验证过→该重验了")。"""\n'
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
        '        """前端启动时轮询: 是否已通过入群验证。\n'
        '        verify_stale=True = 曾验证但已过期(前端提示重验, 不强制弹窗)。\n'
        '        last_qq = 本机上次验证用的 QQ 号(供重验弹窗预填, 免得从头填)。"""\n'
        '        st = _free_verified()\n'
        '        raw = _free_verified_raw()\n'
        '        stale = bool((not st) and raw)\n'
        '        return {"ok": True, "free_build": True, "verified": bool(st),\n'
        '                "verify_stale": stale,\n'
        '                "qq": (st or {}).get("qq", ""),\n'
        '                "last_qq": (raw or {}).get("qq", ""),\n'
        '                "verified_at": (raw or {}).get("verified_at", 0),\n'
        '                "ttl_days": FREE_VERIFY_TTL_DAYS,\n'
        '                "nickname": (st or {}).get("nickname", ""),\n'
        '                "hwid": get_hwid()}\n\n'
        '    def auth_status(self) -> dict:\n'
        '        verified = bool(_free_verified())\n'
        '        raw = _free_verified_raw()\n'
        '        stale = bool((not verified) and raw)\n'
        '        return {"ok": True, "authorized": False, "dev_mode": False,\n'
        '                "free_build": True, "pro_qq": FREE_QQ, "free_verified": verified,\n'
        '                "free_verify_stale": stale,\n'
        '                "nickname": ("免费版" if not verified else\n'
        '                             "训练家_" + (raw.get("qq", "")[-4:])),\n'
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
        '                                  "hwid": get_hwid(),\n'
        '                                  "build_ts": FREE_BUILD_TS,\n'
        '                                  "build_id": FREE_BUILD_ID},\n'
        '                                 ensure_ascii=False).encode("utf-8"),\n'
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
    #    免费版只需补 ClickerMixin(import + 基类列表各一行)
    ("src/gui/bridge.py",
     "from src.gui.bridge_tools import ToolsMixin",
     "from src.gui.bridge_tools import ToolsMixin\nfrom src.gui.bridge_clicker import ClickerService as ClickerMixin  # [CLICKER]",
     "import ClickerMixin(主仓原生 Merchant 时)"),
    ("src/gui/bridge.py",
     "    MerchantMixin,  # [MERCHANT] 远行商人(免费专属)\n):",
     "    MerchantMixin,  # [MERCHANT] 远行商人(免费专属)\n    ClickerMixin,   # [CLICKER] 鼠标连点器(免费专属)\n):",
     "AppBridge 挂 ClickerMixin(主仓原生 Merchant 时)"),
    ("src/gui/bridge.py",
     "from src.gui.bridge_tools import ToolsMixin",
     "from src.gui.bridge_tools import ToolsMixin\nfrom src.gui.bridge_merchant import MerchantService as MerchantMixin  # [MERCHANT]\nfrom src.gui.bridge_clicker import ClickerService as ClickerMixin  # [CLICKER]",
     "import MerchantMixin + ClickerMixin(主仓无 Merchant 回退锚点)"),
    ("src/gui/bridge.py",
     "        self._load_throw_config()",
     "        self._load_throw_config()\n        # [MERCHANT] 远行商人服务状态初始化(纯缓存字段, 无副作用)\n        try:\n            self.merchant_init()\n        except Exception:\n            pass\n        # [CLICKER] 连点器服务(免费专属): 热键注册失败不影响主功能\n        try:\n            from src.gui.bridge_clicker import ClickerService\n            self.clicker = ClickerService(log=self._enqueue_log)\n            self.clicker.install_hotkeys()\n        except Exception as _e:\n            self.clicker = None\n            self._enqueue_log(f'连点器初始化失败: {_e}', 'warning')",
     "AppBridge.__init__ 调 merchant_init + 连点器初始化"),
    # 2) [MERCHANT] 数据源接口保护闸: merchant_fetch 在线拉聚合页, 未入群验证一律拒绝
    #    (防倒卖: 商人聚合源端点藏在闭源 bridge_merchant.py 里, 不给未验证用户消费)
    ("src/gui/bridge_merchant.py",
     "    def merchant_fetch(self, force: bool = False) -> dict:",
     "    def merchant_fetch(self, force: bool = False) -> dict:\n"
     "        gate = self._free_gate()  # [FREE BUILD] 入群验证闸(接口保护防倒卖)\n"
     "        if gate:\n"
     "            return {\"success\": False, \"message\": gate[\"message\"],\n"
     "                    \"free_verify_required\": True}",
     "merchant_fetch 补入群验证闸(接口保护)"),
    # 3) [MERCHANT] 历史查询同闸(历史记录也来自抓取数据)
    ("src/gui/bridge_merchant.py",
     "    def merchant_history(self) -> dict:",
     "    def merchant_history(self) -> dict:\n"
     "        gate = self._free_gate()  # [FREE BUILD] 入群验证闸(接口保护防倒卖)\n"
     "        if gate:\n"
     "            return {\"success\": False, \"message\": gate[\"message\"],\n"
     "                    \"free_verify_required\": True}",
     "merchant_history 补入群验证闸(接口保护)"),
    # 4) [MERCHANT] merchant.js: fetch 被闸拒绝时弹验证框(与 pvp.js 同款联动)
    ("src/gui/web/assets/merchant.js",
     "        const r = await window.pywebview.api.merchant_fetch(!!force);\n"
     "        if (r && r.success) {",
     "        const r = await window.pywebview.api.merchant_fetch(!!force);\n"
     "        if (r && r.free_verify_required) {  // [FREE BUILD] 未验证 → 弹入群验证\n"
     "            if (window.freeVerifyFlow) freeVerifyFlow();\n"
     "            if (grid) grid.innerHTML = '<div class=\"bag-empty\">请先完成入群验证 (点左侧头像)</div>';\n"
     "            return;\n"
     "        }\n"
     "        if (r && r.success) {",
     "merchant.js 未验证弹验证引导"),
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
    // ---- 入群验证弹层(QQ号 + 群口令双输入; 复用主仓 custom-modal-panel 体系样式) ----
    var _freeVerifyOverlay = null;
    function freeVerifyEnsureOverlay() {
        if (_freeVerifyOverlay) return _freeVerifyOverlay;
        var ov = document.createElement('div');
        ov.className = 'custom-modal-overlay';
        ov.id = 'freeVerifyOverlay';
        ov.innerHTML = '<div class="custom-modal-panel" style="width:360px">'
            + '<div class="custom-modal-head"><h3>入群验证</h3>'
            + '<button class="custom-modal-close" id="freeVfClose">×</button></div>'
            + '<div class="custom-modal-body">'
            + '<p>PVP 实时识别需要完成免费的入群验证。<br>'
            + 'QQ 号用于绑定本机(一号一机), 群口令见 QQ 群公告。</p>'
            + '<div id="freeVfTip" style="display:none;margin-top:8px;padding:7px 10px;border-radius:8px;'
            + 'font-size:11.5px;line-height:1.6;color:#fde68a;background:rgba(245,158,11,.12);'
            + 'border:1px solid rgba(245,158,11,.35)"></div>'
            + '<div id="freeVfErr" style="display:none;margin-top:8px;padding:7px 10px;border-radius:8px;'
            + 'font-size:11.5px;line-height:1.6;color:#fca5a5;background:rgba(239,68,68,.12);'
            + 'border:1px solid rgba(239,68,68,.4)"></div>'
            + '<div style="margin-top:10px;display:flex;flex-direction:column;gap:8px">'
            + '<input type="text" id="freeVfQq" class="form-input custom-modal-input" placeholder="QQ 号" maxlength="12" autocomplete="off">'
            + '<input type="text" id="freeVfKey" class="form-input custom-modal-input" placeholder="QQ 群口令(见群公告)" autocomplete="off">'
            + '</div></div>'
            + '<div class="custom-modal-foot">'
            + '<button class="btn btn-ghost" id="freeVfCancel">取消</button>'
            + '<button class="btn btn-primary" id="freeVfGo">验证</button>'
            + '</div></div>';
        document.body.appendChild(ov);
        _freeVerifyOverlay = ov;
        ov.querySelector('#freeVfCancel').onclick = function () { ov.classList.remove('show'); };
        ov.querySelector('#freeVfClose').onclick = function () { ov.classList.remove('show'); };
        ov.onclick = function (e) { if (e.target === ov) ov.classList.remove('show'); };
        return ov;
    }
    function freeVfShowErr(ov, msg, level) {
        var errEl = ov.querySelector('#freeVfErr');
        if (errEl) {
            errEl.textContent = msg;
            errEl.style.display = 'block';
            errEl.style.color = level === 'warning' ? '#fde68a' : '#fca5a5';
            errEl.style.background = level === 'warning' ? 'rgba(245,158,11,.12)' : 'rgba(239,68,68,.12)';
            errEl.style.borderColor = level === 'warning' ? 'rgba(245,158,11,.35)' : 'rgba(239,68,68,.4)';
        }
        if (window.showToast) showToast(msg, level || 'error');
    }
    async function freeVerifyFlow() {
        var ov = freeVerifyEnsureOverlay();
        var qqEl = ov.querySelector('#freeVfQq'), keyEl = ov.querySelector('#freeVfKey');
        var errEl = ov.querySelector('#freeVfErr'), tipEl = ov.querySelector('#freeVfTip');
        // 每次打开都重查验证态: 已验证不打扰; 曾验证则预填 QQ + 过期提示
        var st = null;
        try { st = await pywebview.api.free_verify_status(); } catch (e) { /* web 模式无桩 */ }
        if (st && st.verified) {
            if (window.showToast) showToast('本机已通过入群验证, 无需重复验证', 'success');
            return;
        }
        errEl.style.display = 'none'; tipEl.style.display = 'none';
        keyEl.value = '';
        qqEl.value = (st && st.last_qq) || '';
        if (st && st.verify_stale) {
            tipEl.textContent = '上次验证已过期(有效期 ' + (st.ttl_days || 30) + ' 天), '
                + 'QQ 号已帮你填好, 重新输入当前群口令即可';
            tipEl.style.display = 'block';
        }
        ov.classList.add('show');
        setTimeout(function () { (qqEl.value ? keyEl : qqEl).focus(); }, 60);
        var goBtn = ov.querySelector('#freeVfGo');
        goBtn.onclick = async function () {
            var qq = qqEl.value.trim(), key = keyEl.value.trim();
            errEl.style.display = 'none';
            if (!/^\d{5,12}$/.test(qq)) { freeVfShowErr(ov, '请填写正确的 QQ 号', 'warning'); return; }
            if (!key) { freeVfShowErr(ov, '请填写群口令(见 QQ 群公告)', 'warning'); return; }
            goBtn.disabled = true; goBtn.textContent = '验证中…';
            try {
                var r = await pywebview.api.free_verify_group(qq, key);
                if (r && r.ok && r.verified) {
                    ov.classList.remove('show');
                    if (window.showToast) showToast(r.message || '验证通过', 'success');
                    window.FREE_VERIFIED = true;
                    window.FREE_VERIFY_STALE = false;
                    if (window.freeApplyVerifyUI) freeApplyVerifyUI(true);
                } else {
                    freeVfShowErr(ov, (r && r.message) || '验证失败: 口令不匹配或服务不可达, 请核对群公告后重试', 'error');
                }
            } catch (e) {
                freeVfShowErr(ov, '验证异常: ' + e, 'error');
            }
            goBtn.disabled = false; goBtn.textContent = '验证';
        };
        keyEl.onkeydown = function (e) { if (e.key === 'Enter') goBtn.click(); };
        qqEl.onkeydown = function (e) { if (e.key === 'Enter') keyEl.focus(); };
    }
    window.freeVerifyFlow = freeVerifyFlow;
    // ---- isAuthed 语义对齐免费版: 通过过入群验证 = 已授权 ----
    // (主仓 isAuthed 读 authState.authorized, 免费树无 auth 体系恒 false →
    //  PVP 预设保存/图鉴训练卡全被「登录后可保存」卡死; 改读验证态)
    // 过期不连坐保存类: FREE_VERIFY_STALE(曾验证但过期)也算已授权 —
    // 过期只重锁抓包识别类功能(后端 _free_gate 管), 保存类免费功能照常可用。
    // 注: 付费页(auto/aipvp/daily)拦截由 FREE freeNav 的 onclick 覆写兜底,
    //     不依赖 isAuthed — 因此此处放开不会误开付费功能。
    Object.defineProperty(window, "isAuthed", {
        get: function () {
            return function () { return !!(window.FREE_VERIFIED || window.FREE_VERIFY_STALE); };
        },
        configurable: true,
    });
    // ---- PVP 页锁幕: 未验证盖"入群验证"引导(后端 _free_gate 同步硬闸) ----
    // 过期重验 vs 从未验证文案区分; 点按钮走 freeVerifyFlow(QQ 号自动预填)
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
        var stale = !!window.FREE_VERIFY_STALE;
        if (veil) {
            // 已有锁幕但状态切换(如从未验证→已过期): 刷新文案
            var t = veil.querySelector('.av-title'), d = veil.querySelector('.av-desc');
            if (t) t.textContent = stale ? '入群验证已过期' : 'PVP 识别需要入群验证';
            if (d) d.innerHTML = stale
                ? '上次验证已过期(有效期 ' + (window.FREE_VERIFY_TTL || 30) + ' 天), 重新输入当前群口令即可<br>QQ 号已记住了, 点下面按钮自动填好 · 口令见 QQ 群公告'
                : '验证完全免费: 输入 QQ 号 + 群口令即可<br>口令见 QQ 群公告 · 一号绑定一机 · 本机数据不上传';
            var vb = veil.querySelector('.av-box .btn');
            if (vb) vb.textContent = stale ? '重新验证 (免费)' : '立即验证 (免费)';
            return;
        }
        veil = document.createElement('div');
        veil.className = 'auth-veil free-verify-veil';
        veil.innerHTML = '<div class="av-box">'
            + '<div class="av-icon">' + (window.icon ? icon('lock', 36) : '') + '</div>'
            + '<div class="av-title">' + (stale ? '入群验证已过期' : 'PVP 识别需要入群验证') + '</div>'
            + '<div class="av-desc">' + (stale
                ? '上次验证已过期(有效期 ' + (window.FREE_VERIFY_TTL || 30) + ' 天), 重新输入当前群口令即可<br>QQ 号已记住了, 点下面按钮自动填好 · 口令见 QQ 群公告'
                : '验证完全免费: 输入 QQ 号 + 群口令即可<br>口令见 QQ 群公告 · 一号绑定一机 · 本机数据不上传')
            + '</div>'
            + '<button class="btn btn-primary" onclick="freeVerifyFlow()">' + (stale ? '重新验证 (免费)' : '立即验证 (免费)') + '</button></div>';
        page.appendChild(veil);
    };
    // 启动时查验证态: 未验证 → pvp 页上锁 + 头像提示"点击验证"
    (function freeVerifyBoot() {
        var ask = function () {
            try {
                pywebview.api.free_verify_status().then(function (st) {
                    var ok = !!(st && st.verified);
                    window.FREE_VERIFIED = ok;
                    window.FREE_VERIFY_STALE = !!(st && st.verify_stale);
                    window.FREE_VERIFY_TTL = (st && st.ttl_days) || 30;
                    if (window.freeApplyVerifyUI) freeApplyVerifyUI(ok);
                    if (!ok) {
                        var sub = document.getElementById('authState');
                        var box = document.getElementById('authAvatarBox');
                        if (window.FREE_VERIFY_STALE) {
                            // 过期: 温和提示重验(不强制弹窗, 仅抓包识别类功能受影响)
                            if (sub) sub.innerHTML = '<span class="auth-exp">验证已过期 · 点击重新验证</span>';
                            if (box) box.title = '入群验证已过期, 点击重新验证(免费, QQ 号自动预填)';
                            if (window.showToast) showToast('入群验证已过期(有效期 ' + window.FREE_VERIFY_TTL + ' 天), 需要时点左上角头像重验, QQ 号会自动填好', 'warning', 8000);
                        } else {
                            if (sub) sub.innerHTML = '<span class="auth-exp">未验证 · 点击验证</span>';
                            if (box) box.title = '点击完成入群验证(免费)';
                        }
                    }
                }).catch(function () {});
            } catch (e) { /* web 模式无 pywebview 桩时静默 */ }
        };
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(ask, 400); });
        else setTimeout(ask, 400);
        // pywebview 桩就绪晚于 DOM 的场景再补一口
        window.addEventListener('pywebviewready', function () { setTimeout(ask, 200); });
    })();
    // 免费版身份显示: 已验证显示昵称, 未验证/过期引导验证(覆盖主仓 renderAuth 的未登录文案)
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
                } else if (window.FREE_VERIFY_STALE) {
                    if (sub) sub.innerHTML = '<span class="auth-exp">验证已过期 · 点击重新验证</span>';
                    if (box) box.title = '入群验证已过期, 点击重新验证(免费, QQ 号自动预填)';
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


def _strip_free_scripts(html: str) -> str:
    """剥掉 index.html 里残留的 FREE_BUILD 内联脚本块(整块移除)。

    主仓 index.html 曾被旧版免费补丁整块污染并误提交; 注入幂等守卫见到
    'FREE_BUILD' 字样就跳过, 导致 dist 永远拿不到新版 FREE 脚本。此处把
    含 FREE BUILD 标记的 <script>...</script> 整块剥掉, 再重新注入最新版。
    同时剥掉散落的 FREE 行内注释锚点不影响(那些是 SOURCE_PATCHES 的目标行,
    属于主仓源码正常补丁, 不在 <script> 整块内)。"""
    import re as _re

    def _is_free_block(m: "_re.Match") -> str:
        return "" if "[FREE BUILD]" in m.group(1) or "window.FREE_BUILD" in m.group(1) else m.group(0)

    stripped = _re.sub(r"<script>(.*?)</script>", _is_free_block, html, flags=_re.S)
    if stripped != html:
        print("[sync_free] 剥离残留 FREE 脚本块(主仓污染) → 将注入最新版")
    return stripped


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
        # 保留运行时用户数据: data/ 是实测产物(抓包帧/对局记录/验证 token),
        # 2026-10-04 实测时被重建误删过一次, 从此移出→移回。
        # 2026-10-05 教训: shutil.move 遇目标文件被运行中服务锁定(如
        # rkpp_server.log)会半途失败 — 部分内容已拷进 keep 但源目录 rmtree
        # 报错退出, 下轮 sync 又把残缺 keep 套进新 keep, 生成嵌套洋葱层。
        # 修复: 1) move 失败时保留现场直接终止(不再生成洋葱);
        #       2) 移回时若 dist_free/data 已存在(上次残留)则先并入。
        data_dir = out_root / "data"
        keep_data = out_root.parent / (out_root.name + "_data_keep")
        if data_dir.exists():
            if keep_data.exists():
                # 上次失败残留: keep 里的内容并入 data(同层同名目录/文件合并)
                import itertools
                for root_dir, _dirs, files in os.walk(keep_data):
                    rel = os.path.relpath(root_dir, keep_data)
                    dst_dir = data_dir / rel
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for f in files:
                        src_f = os.path.join(root_dir, f)
                        dst_f = dst_dir / f
                        # 取更新的一份(抓包日志只增不改, mtime 新者为真)
                        if (not dst_f.exists()
                                or os.path.getmtime(src_f) > os.path.getmtime(dst_f)):
                            shutil.copy2(src_f, dst_f)
                shutil.rmtree(keep_data, ignore_errors=True)
            shutil.move(str(data_dir), str(keep_data))
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
    # 注入前先剥掉残留的 FREE 脚本块(主仓 index.html 曾被旧版免费补丁污染并
    # 误提交, 幂等守卫 "FREE_BUILD not in html" 见到残留就跳过 → dist 永远
    # 拿不到新版补丁)。剥干净再注入, 保证免费树始终带最新版 FREE 脚本。
    idx_html = out_root / "src" / "gui" / "web" / "index.html"
    if idx_html.exists():
        html = idx_html.read_text(encoding="utf-8")
        html = html.replace("洛克王国 · PVP 助手控制台", "洛克小助手 · Free 控制台")
        html = _strip_free_scripts(html)
        html = html.replace("</body>", FREE_PATCH_HTML.replace("FREE_QQ_PLACEHOLDER", FREE_QQ) + "\n</body>", 1)
        idx_html.write_text(html, encoding="utf-8")
        print("[sync_free] 前端补丁 OK: index.html")

    # 导航付费清单改写: NAV_PAID_PAGES 与拆分线保持一致
    # 2026-10-11 主仓 app.js 六文件拆分: 常量随"侧边栏导航排序"落入 app_auth.js
    app_js = out_root / "src" / "gui" / "web" / "assets" / "app_auth.js"
    if app_js.exists():
        js = app_js.read_text(encoding="utf-8")
        old = "const NAV_PAID_PAGES = ['pvp', 'aipvp', 'daily'];"
        new = "const NAV_PAID_PAGES = ['auto', 'aipvp', 'daily']; // [FREE BUILD]"
        if js.count(old) == 1:
            js = js.replace(old, new)
            app_js.write_text(js, encoding="utf-8")
            print("[sync_free] 前端补丁 OK: app_auth.js NAV_PAID_PAGES")
        elif "[FREE BUILD]" in js:
            print("[sync_free] 前端补丁跳过: app_auth.js 已含 FREE 标记")
        else:
            raise SystemExit("[sync_free] app_auth.js 锚点失效: NAV_PAID_PAGES 未命中, 请核对主仓改动")

    # 免费版已删除页面的前端 section/资源保留无妨(导航已拦), 但 pve 数据层不入树已由排除清单保证

    # 归还 Tauri 编译缓存(见 main() 开头的移出逻辑)
    keep_dir = out_root.parent / (out_root.name + "_target_keep")
    if keep_dir.exists():
        (out_root / "src-tauri").mkdir(parents=True, exist_ok=True)
        shutil.move(str(keep_dir), str(out_root / "src-tauri" / "target"))
        print("[sync_free] Tauri 编译缓存已保留(增量编译)")

    # 归还运行时用户数据(见 main() 开头的移出逻辑);
    # 2026-10-05: 若 data 已存在(上次 sync 中断残留)则并入后再移除 keep
    keep_data = out_root.parent / (out_root.name + "_data_keep")
    if keep_data.exists():
        data_dir = out_root / "data"
        if data_dir.exists():
            for root_dir, _dirs, files in os.walk(keep_data):
                rel = os.path.relpath(root_dir, keep_data)
                dst_dir = data_dir / rel
                dst_dir.mkdir(parents=True, exist_ok=True)
                for f in files:
                    src_f = os.path.join(root_dir, f)
                    dst_f = dst_dir / f
                    if (not dst_f.exists()
                            or os.path.getmtime(src_f) > os.path.getmtime(dst_f)):
                        shutil.copy2(src_f, dst_f)
            shutil.rmtree(keep_data, ignore_errors=True)
        else:
            shutil.move(str(keep_data), str(data_dir))
        print("[sync_free] 运行时数据已保留(data/ 抓包帧/对局/验证态)")

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
