# -*- coding: utf-8 -*-
"""sync_free.py — 从主仓生成免费版代码树（manifest 驱动）

用法:
    python tools/sync_free.py --main <主仓路径> --out <输出目录> [--dry-run]

流程: 拷贝主仓 → 应用排除清单 → 强制包含免费依赖 → 写入付费模块存根
     → 前端 index.html 注入 FREE_BUILD 补丁 → 产出 Free 树。
状态: v0.2。首次分发前必须:
  1) --dry-run 核对排除清单没漏主仓新增的付费/敏感文件
  2) python tools/check_imports.py <dist_free> 复查导入图
  3) py_compile 冒烟 + 实机启动验证
本脚本与产出树永不包含密钥、抓包与授权逻辑（见 docs/功能拆分.md）。
"""
import argparse
import shutil
from pathlib import Path

# ---------------- 排除清单 ----------------
# 目录级排除（相对主仓根；带斜杠=精确前缀，不带=任意层级段名命中即排除）
EXCLUDE_DIRS = [
    "server/",           # 顶层授权服务端（worker.js 等; 根级前缀, 不误伤 src/server）
    "src/capture",       # 抓包 + 截屏后端（仅强制包含免费所需, 见 INCLUDE_FORCE）
    "src/pvp",           # PVP 引擎 / 协议解码 / 加密数据（仅 roi_template 强制包含）
    "src/tasks",         # 日常任务执行器（付费）
    "build", "dist",     # 构建产物
    ".git", "__pycache__", ".vscode", ".idea", ".zcode",
    ".venv", "venv",               # 虚拟环境
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "htmlcov",
    "node_modules", ".github",
    "data",              # 运行数据（历史库/截图/抓包缓存）
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
    # 授权体系
    "src/gui/auth.py",
    "src/gui/auth_core.py",
    "src/gui/auth_core.pyd",
    "src/gui/bridge_auth.py",
    # 付费 Mixin（只出存根, 不拷原文件; 见 STUBS）
    "src/gui/bridge_pvp.py",
    "src/gui/bridge_pvp_data.py",
    "src/gui/bridge_daily.py",
    # 抓包工具
    "tests/test_capture.py",
    "tools/capture_game_window.py",
    "tools/capture_screen.py",
    "tools/check_capture_env.py",
    "tools/pvp_packet_capture.py",
    "tools/snip_capture.py",
    # 构建链与密钥
    "tools/build_hardened.py",
    "tools/gen_worker_keys.js",
    "tools/pvp_mcp_server.py",
    "keys.json",
]

# 强制包含（位于被排除目录内, 但免费功能依赖的文件; 优先于目录排除）
# 依据 tools/check_imports.py 报告: 丢球/挂机/视觉/窗口管理懒导入这些模块
INCLUDE_FORCE = [
    "src/capture/__init__.py",        # 仅 docstring, 已核验
    "src/capture/window_capture.py",  # win32 窗口截屏, 无 src.* 依赖, 已核验
    "src/capture/fast_capture.py",    # mss 快速截屏, 无 src.* 依赖, 已核验
    # 注意: src/pvp/__init__.py 不在强制清单 — 它顶层 re-export 付费模块,
    # 由 STUBS 写入空存根代替
]

# ---------------- 存根 ----------------
# 免费树中的同名空实现（保持类名/包名兼容, 主仓代码零改动）
STUBS = {
    # -- 桥接层付费 Mixin: __getattr__ 兜底, 缺失方法统一返回 Pro 提示 --
    "src/gui/bridge_pvp.py": (
        '"""[Free 构建存根] PVP 识别引擎/采集/推演属 Pro 版。"""\n\n\n'
        'class PvpEngineMixin:\n'
        '    """Free 构建空实现: 缺失属性统一返回 Pro 提示。"""\n\n'
        '    def pvp_engine_stop(self):\n'
        '        """停机路径友好: 无引擎可停。"""\n'
        '        return {"ok": True, "message": "免费版无 PVP 引擎"}\n\n'
        '    def __getattr__(self, name):\n'
        '        def _pro_only(*a, **k):\n'
        '            return {"ok": False, "message": f"{name} 为 Pro 功能, 免费版不含"}\n'
        '        return _pro_only\n'
    ),
    "src/gui/bridge_pvp_data.py": (
        '"""[Free 构建存根] PVP 数据/图鉴/战报属 Pro 版。"""\n\n\n'
        'class PvpDataMixin:\n'
        '    """Free 构建空实现: 缺失属性统一返回 Pro 提示。"""\n\n'
        '    def __getattr__(self, name):\n'
        '        def _pro_only(*a, **k):\n'
        '            return {"ok": False, "message": f"{name} 为 Pro 功能, 免费版不含"}\n'
        '        return _pro_only\n'
    ),
    "src/gui/bridge_daily.py": (
        '"""[Free 构建存根] 日常任务托管属 Pro 版。"""\n\n\n'
        'class DailyMixin:\n'
        '    """Free 构建空实现: 缺失属性统一返回 Pro 提示。"""\n\n'
        '    def __getattr__(self, name):\n'
        '        def _pro_only(*a, **k):\n'
        '            return {"ok": False, "message": f"{name} 为 Pro 功能, 免费版不含"}\n'
        '        return _pro_only\n'
    ),
    # -- 授权体系: 免费版无卡密/账号, 闸门全放行 --
    "src/gui/bridge_auth.py": (
        '"""[Free 构建存根] 免费版无授权体系: 闸门全放行, 付费 RPC 返回提示。"""\n\n\n'
        'class AuthUpdateMixin:\n'
        '    """Free 构建授权存根。"""\n\n'
        '    def _auth_gate(self):\n'
        '        """免费版无付费闸门: 全量放行。"""\n'
        '        return None\n\n'
        '    def auth_status(self):\n'
        '        return {"ok": True, "authorized": True, "dev_mode": False,\n'
        '                "nickname": "免费版", "expires_at": 0, "free_build": True}\n\n'
        '    def auth_activate(self, *a, **k):\n'
        '        return {"ok": False, "message": "免费版无需激活"}\n\n'
        '    def auth_logout(self):\n'
        '        return {"ok": True, "message": "免费版无需退出"}\n\n'
        '    def auth_verify_remote(self):\n'
        '        return {"ok": True, "authorized": True, "nickname": "免费版"}\n\n'
        '    def start_auth_verify(self):\n'
        '        pass\n\n'
        '    def _push_auth_state(self):\n'
        '        pass\n\n'
        '    def auth_account_login(self, *a, **k):\n'
        '        return {"ok": False, "message": "免费版无需登录"}\n\n'
        '    def auth_account_register(self, *a, **k):\n'
        '        return {"ok": False, "message": "免费版无需注册"}\n\n'
        '    def auth_account_sendcode(self, *a, **k):\n'
        '        return {"ok": False, "message": "免费版无需验证"}\n\n'
        '    def auth_account_reset(self, *a, **k):\n'
        '        return {"ok": False, "message": "免费版无需重置"}\n'
    ),
    # -- 包初始化: 主仓 src/pvp/__init__ 顶层 re-export 付费模块, 换空存根 --
    "src/pvp/__init__.py": (
        '"""[Free 构建存根] 免费版不含 PVP 数据层; 仅保留 roi_template(视觉调试台)。"""\n'
    ),
    # -- 服务层付费存根（方法名镜像主仓, RPC dispatch 语义一致） --
    "src/server/services/pvp_service.py": (
        '"""[Free 构建存根] PVP 服务属 Pro 版。"""\n\n\n'
        'class PvpService:\n'
        '    """Free 构建空实现: 仅保留日志口。"""\n\n'
        '    def __init__(self, on_log=None, **_):\n'
        '        self._on_log = on_log or (lambda *a, **k: None)\n\n'
        '    def log(self, msg, level="info"):\n'
        '        self._on_log(str(msg), level)\n'
    ),
    "src/server/services/daily_service.py": (
        '"""[Free 构建存根] 日常任务服务属 Pro 版（方法名镜像主仓）。"""\n\n\n'
        '_PRO = {"ok": False, "message": "日常任务为 Pro 功能, 免费版不含"}\n\n\n'
        'class DailyService:\n'
        '    """Free 构建空实现: 日常/花种/图鉴全部返回 Pro 提示。"""\n\n'
        '    def __init__(self, on_log=None, capture_frame_cb=None,\n'
        '                 game_launch_cb=None, **_):\n'
        '        self._on_log = on_log or (lambda *a, **k: None)\n\n'
        '    def daily_list(self):\n'
        '        return {"ok": False, "list": [], "message": _PRO["message"]}\n\n'
        '    def daily_save(self, *a, **k):\n'
        '        return dict(_PRO)\n\n'
        '    def daily_run(self, *a, **k):\n'
        '        return dict(_PRO)\n\n'
        '    def daily_run_queue(self, *a, **k):\n'
        '        return dict(_PRO)\n\n'
        '    def daily_stop(self):\n'
        '        return {"ok": True, "message": "免费版无运行中的日常任务"}\n\n'
        '    def daily_status(self):\n'
        '        return {"ok": True, "running": False, "state": "free",\n'
        '                "message": "免费版不含日常任务"}\n\n'
        '    def flower_config_load(self):\n'
        '        return dict(_PRO)\n\n'
        '    def flower_config_save(self, *a, **k):\n'
        '        return dict(_PRO)\n\n'
        '    def pokedex_data(self):\n'
        '        return {"ok": False, "message": "图鉴数据为 Pro 功能"}\n'
    ),
}

# ---------------- 免费版前端补丁 ----------------
# 注入 index.html（</body> 前）: 隐藏付费导航项并重编号, 覆盖头像点击。
# 免费用户(多数)全程看不到付费入口, 也无需登录。
FREE_PATCH_HTML = """<script>
/* [FREE BUILD] 免费版补丁: 隐藏付费导航项, 免登录提示 */
window.FREE_BUILD = true;
(function () {
    'use strict';
    var PAID = '.nav-item[data-page="pvp"],.nav-item[data-page="aipvp"],'
             + '.nav-item[data-page="daily"]';
    function freeHidePaid() {
        document.querySelectorAll(PAID).forEach(function (b) {
            b.style.display = 'none';
        });
        ['navProSep', 'navProCap'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
        var n = 0;
        document.querySelectorAll('.nav .nav-item').forEach(function (b) {
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
            freeHidePaid();
            return r;
        };
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', freeHidePaid);
    } else {
        freeHidePaid();
    }
    window.authClick = function () {
        if (window.showToast) showToast('免费版无需登录, 已包含功能全部可用', 'info');
    };
})();
</script>"""


def _excluded(rel: Path) -> bool:
    """排除判定: 简单名按路径段匹配, 带斜杠的按 POSIX 前缀匹配"""
    posix = rel.as_posix()
    for d in EXCLUDE_DIRS:
        if d.endswith("/"):
            # 根级目录前缀（如 "server/" 只排顶层 server, 不伤 src/server）
            if posix.startswith(d):
                return True
        elif "/" in d:
            if posix == d or posix.startswith(d + "/"):
                return True
        elif d in rel.parts:
            return True
    return posix in EXCLUDE_FILES


def iter_source_files(main_root: Path):
    """按排除清单产出 (相对路径, 源文件) 对; INCLUDE_FORCE 优先于目录排除"""
    for p in sorted(main_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(main_root)
        if rel.as_posix() in INCLUDE_FORCE:
            yield rel, p
            continue
        if _excluded(rel):
            continue
        yield rel, p


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
    all_items = copied + [Path(r) for r in STUBS]

    print(f"[sync_free] 计划: 拷贝 {len(copied)} 个文件 + {len(STUBS)} 个存根")
    if args.dry_run:
        for rel in sorted(all_items, key=lambda p: p.as_posix()):
            tag = "STUB" if rel.as_posix() in STUBS else "COPY"
            print(f"  {tag} {rel.as_posix()}")
        return

    if out_root.exists():
        shutil.rmtree(out_root)
    for rel, src in iter_source_files(main_root):
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for rel, content in STUBS.items():
        dst = out_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")

    # 免费版前端补丁: index.html 注入 FREE_BUILD 脚本（</body> 前）
    idx_html = out_root / "src" / "gui" / "web" / "index.html"
    if idx_html.exists():
        html = idx_html.read_text(encoding="utf-8")
        if "FREE_BUILD" not in html:
            html = html.replace("</body>", FREE_PATCH_HTML + "\n</body>", 1)
            idx_html.write_text(html, encoding="utf-8")

    marker = out_root / "FREE_BUILD"
    marker.write_text(
        "洛克小助手 Free 构建产物 — 更新通道: github.com/jxylisty/roco-helper-free\n",
        encoding="utf-8")
    print(f"[sync_free] 免费树已生成: {out_root}")
    print("[sync_free] TODO: compileall 冒烟 + check_imports 复查 + 启动验证通过后才可分发")


if __name__ == "__main__":
    main()
