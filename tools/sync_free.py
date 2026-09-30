# -*- coding: utf-8 -*-
"""sync_free.py — 从主仓生成免费版代码树（manifest 驱动）

用法:
    python tools/sync_free.py --main <主仓路径> --out <输出目录> [--dry-run]

流程: 拷贝主仓 → 应用排除清单 → 写入付费模块存根 → 产出 Free 树。
状态: v0 脚手架。首次真实构建前必须:
  1) --dry-run 核对排除清单没漏主仓新增的付费/敏感文件
  2) 补齐 STUBS 中标记 TODO 的存根（按主仓 Mixin 真实方法签名）
  3) 对产出树 py_compile 冒烟 + 实机启动验证
本脚本与产出树永不包含密钥、授权与抓包逻辑（见 docs/功能拆分.md 永不开源清单）。
"""
import argparse
import shutil
from pathlib import Path

# 目录级排除（相对主仓根；带斜杠=精确前缀，不带=任意层级段名命中即排除）
EXCLUDE_DIRS = [
    "server",            # 授权服务端（worker.js 等）
    "src/capture",       # 抓包
    "src/pvp",           # PVP 引擎 / 协议解码 / 加密数据
    "build", "dist",     # 构建产物
    ".git", "__pycache__", ".vscode", ".idea", ".zcode",
    ".venv", "venv",               # 虚拟环境
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", ".github",
    "data",              # 运行数据（历史库/截图/抓包缓存）
    "docs",              # 主仓内部文档（本仓库有自己的 docs）
    "output",            # 对局截图/敌方头像/OCR 失败帧（含用户对局数据, 绝不开源）
    "src-tauri/target",  # Rust 构建缓存
    "src-tauri/gen",     # Tauri 生成物
    "tools/_diag_out", "tools/_replay_out",
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

# 存根: 免费树中的同名空实现（保持类名兼容, 主仓 bridge.py 零改动）
# TODO(首次构建): 按主仓各 Mixin 的真实方法签名补齐空方法
STUBS = {
    "src/gui/bridge_pvp.py": (
        '"""[Free 构建存根] PVP 全家桶属 Pro 版。"""\n\n\n'
        'class PvpEngineMixin:\n'
        '    """Free 构建空实现（TODO 首次构建核对方法签名）。"""\n'
    ),
    "src/gui/bridge_pvp_data.py": (
        '"""[Free 构建存根] PVP 数据/战报属 Pro 版。"""\n\n\n'
        'class PvpDataMixin:\n'
        '    """Free 构建空实现（TODO 首次构建核对方法签名）。"""\n'
    ),
    "src/gui/bridge_daily.py": (
        '"""[Free 构建存根] 日常托管属 Pro 版。"""\n\n\n'
        'class DailyMixin:\n'
        '    """Free 构建空实现（TODO 首次构建核对方法签名）。"""\n'
    ),
    "src/gui/bridge_auth.py": (
        '"""[Free 构建存根] 授权体系不进免费版。"""\n\n\n'
        'class AuthUpdateMixin:\n'
        '    """Free 构建空实现（TODO 首次构建核对方法签名）。"""\n'
    ),
}


def _excluded(rel: Path) -> bool:
    """排除判定: 简单名按路径段匹配, 带斜杠的按 POSIX 前缀匹配"""
    posix = rel.as_posix()
    for d in EXCLUDE_DIRS:
        if "/" in d:
            if posix == d or posix.startswith(d + "/"):
                return True
        elif d in rel.parts:
            return True
    return posix in EXCLUDE_FILES


def iter_source_files(main_root: Path):
    """按排除清单产出 (相对路径, 源文件) 对"""
    for p in sorted(main_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(main_root)
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

    print(f"[sync_free] 免费树已生成: {out_root}")
    print("[sync_free] TODO: py_compile 冒烟 + 实机启动验证通过后才可分发")


if __name__ == "__main__":
    main()
