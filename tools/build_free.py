#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_free.py — 免费版一键出包。

一次跑完免费版完整流水线，替代此前逐个手敲的 6 条命令：

    sync_free.py        → 生成免费代码树(含补丁/存根/排除付费层)
    + rkpp_clean        → 放入解码器(并把本机会话密钥挡在包外)
    + redist            → 放入环境安装器(VC++ 运行库 / WebView2)
    build_free_python   → 装内嵌 Python 运行时
    compile_free_pyd    → Cython 加固(核心算法明文 .py → .pyd)
    cargo build         → 编译 Tauri 壳 exe
    zip                 → 清理 + 组包 + 泄漏终检

用法:
    python tools/build_free.py                     # 全流程
    python tools/build_free.py --skip-exe          # 复用上次 exe(只改 Python 侧时)
    python tools/build_free.py --from cython       # 从某阶段起跑(断点续跑)

设计取舍(踩过的坑都固化在这里):
1. **不打 --target 覆盖** —— 各阶段用自己的默认目录。cargo 缓存由 sync_free
   在重建树时保留(src-tauri/target 移出→移回), 改前端后增量编译约 4 分钟。
   绝不复用主仓 target: 免费 exe 会盖掉付费版 exe, 下次出付费包会打错壳。
2. **rkpp_clean/Key/latest.key 必须删** —— 它是构建机账号的会话密钥,
   跟着包发出去等于把密钥给对方。缺失时 load_key_from_file 返回 None,
   解码器自动降级为现场捕获握手, 功能不受影响。
3. **redist 必须入包** —— 用户的机器不一定有 VC++ 运行库, 缺了主程序直接
   起不来。付费版 build_release.py 缺 redist 会 die(), 免费版此前完全没这一步。
4. **打 zip 前做泄漏终检** —— auth.json / dev_mode.marker / CLAUDE.local.md /
   latest.key / .git 任何一项命中直接失败, 不允许"打完再发现"。
5. 输出目录被占用时先结束残留进程 —— 测试启动的后端子进程会锁住
   rkpp_server.log, 直接 rmtree 会报 Device or resource busy。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# Windows 控制台默认 GBK, 打印 ✓/⚠ 会 UnicodeEncodeError 打断长时构建
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TOOLS = Path(__file__).resolve().parent
FREE_REPO = TOOLS.parent
DEFAULT_MAIN = Path(r"D:\洛克王国ai\lkwgai_pvp_assistant")
DEFAULT_RKPP = Path(r"D:\洛克王国ai\rkpp_clean")
DEFAULT_OUT = FREE_REPO / "洛克小助手Free"

STEPS = ["sync", "runtime", "python", "cython", "exe", "zip"]
STEP_LABELS = {
    "sync": "生成免费代码树 + 解码器 + 环境安装器",
    "runtime": "(并入 sync)",
    "python": "安装内嵌 Python 运行时",
    "cython": "Cython 加固核心算法",
    "exe": "编译 Tauri 壳 exe",
    "zip": "泄漏终检 + 组包",
}

# 这些文件一旦出现在包内就是泄漏, 硬失败而不是警告
LEAK_PATTERNS = ("auth.json", "dev_mode.marker", "claude.local.md", "latest.key", ".git")


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    raise SystemExit(f"[build_free] 失败: {msg}")


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    log("    $ " + " ".join(str(c) for c in cmd))
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                          env=merged)
    if proc.returncode != 0:
        die(f"命令失败 (exit {proc.returncode}): {' '.join(str(c) for c in cmd)}")


def _port_owner(port: int) -> str:
    """返回占用该端口的进程描述; 空闲返回空串。"""
    try:
        import psutil
    except ImportError:
        return ""
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                pid = conn.pid
                if not pid:
                    return f"PID {conn.pid}"
                try:
                    proc = psutil.Process(pid)
                    return f"PID {pid} {proc.name()} — {proc.exe()}"
                except psutil.Error:
                    return f"PID {pid}"
    except (psutil.Error, OSError):
        return ""
    return ""


def guard_port(port: int = 17366) -> None:
    """cython 阶段的冒烟会真起服务, 端口被占会直接失败。

    最常见的来源是用户正拿着旧包在实测 —— 这不是脚本能自行解决的情况,
    必须明确告诉对方先关掉, 否则只会看到一句「端口已占用」无从下手。
    """
    owner = _port_owner(port)
    if not owner:
        return
    die(f"端口 {port} 已被占用, 冒烟无法启动服务。\n"
        f"       占用者: {owner}\n"
        f"       → 请先退出正在运行的洛克小助手(旧包实例), 再重新出包。\n"
        f"         若确认该进程无关, 关掉它或用 --from exe 跳过编译阶段。")


# ---------------------------------------------------------------- 进程与目录

def kill_stragglers(out_root: Path) -> None:
    """结束从输出目录启动的残留进程(测试跑过的后端子进程会锁文件)。"""
    try:
        import psutil
    except ImportError:
        return
    target = str(out_root.resolve()).lower()
    killed = []
    for proc in psutil.process_iter(("pid", "cwd", "cmdline")):
        try:
            info = proc.info
            argv = info.get("cmdline") or []
            cwd = (info.get("cwd") or "").lower()
            line = " ".join(argv).lower()
            if target in cwd or target in line:
                proc.kill()
                killed.append(info["pid"])
        except (psutil.Error, OSError):
            continue
    if killed:
        log(f"    已结束残留进程: {killed}")
        time.sleep(1.5)


def fresh_dir(path: Path) -> None:
    if path.exists():
        kill_stragglers(path)
        for attempt in range(3):
            try:
                shutil.rmtree(path)
                break
            except OSError:
                if attempt == 2:
                    die(f"无法删除 {path} (文件被占用? 先关掉正在运行的助手)")
                time.sleep(2)


# ---------------------------------------------------------------- 各阶段

def step_sync(out_root: Path, main: Path, rkpp: Path) -> None:
    log(f"\n[1/6] {STEP_LABELS['sync']}")
    if not out_root.exists():
        run([sys.executable, TOOLS / "sync_free.py", "--main", main, "--out", out_root])
    else:
        run([sys.executable, TOOLS / "sync_free.py", "--main", main, "--out", out_root])

    # 解码器: 只带运行时需要的文件, 排除 tests/tools/.git/__pycache__
    dst = out_root / "rkpp_clean"
    dst.mkdir(parents=True, exist_ok=True)
    allowed_suffix = {".py", ".sqlite", ".md"}
    allowed_names = {"LICENSE", "NOTICE"}
    copied = 0
    for src in rkpp.iterdir():
        if not src.is_file():
            continue
        if src.suffix in allowed_suffix or src.name in allowed_names:
            shutil.copy2(src, dst / src.name)
            copied += 1
    if not (dst / "rkpp_live_tools.py").is_file():
        die(f"解码器拷贝不完整, 缺 rkpp_live_tools.py → {dst}")
    log(f"    ✓ rkpp_clean: {copied} 个文件")

    # 会话密钥必须挡在包外 —— 这是构建机自己的 key
    key_dir = dst / "Key"
    if key_dir.exists():
        shutil.rmtree(key_dir)
        log("    ✓ 已移除 rkpp_clean/Key (构建机会话密钥不入包)")

    # 环境安装器: 缺 VC++ 运行库用户主程序起不来
    redist_src = main / "data" / "redist"
    if not (redist_src / "环境安装.bat").is_file():
        die(f"缺 {redist_src / '环境安装.bat'} — 免费包必须带环境安装器")
    if not (redist_src / "check_driver.ps1").is_file():
        die(f"缺 {redist_src / 'check_driver.ps1'}")
    shutil.copytree(redist_src, out_root / "redist",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    size = sum(f.stat().st_size for f in (out_root / "redist").rglob("*") if f.is_file())
    log(f"    ✓ redist/ (环境安装.bat + VC++/WebView2 安装器) {size / 1e6:.0f} MB")


def step_python(out_root: Path, nuget: Path, wheelhouse: Path | None) -> None:
    log(f"\n[2/6] {STEP_LABELS['python']}")
    if (out_root / "python").exists():
        log("    python/ 已存在, 跳过(要重装请先删掉)")
        return
    if not nuget.is_file():
        die(f"缺内嵌 Python 包: {nuget} (从 python.org 下 NuGet 包放到 dist/)")
    cmd = [sys.executable, TOOLS / "build_free_python.py",
           "--nuget", nuget, "--free-root", out_root]
    if wheelhouse and wheelhouse.is_dir():
        cmd += ["--wheelhouse", wheelhouse]
    run(cmd, cwd=FREE_REPO)


def step_cython(out_root: Path, main: Path) -> None:
    log(f"\n[3/6] {STEP_LABELS['cython']}")
    build_py = main / ".venv-build" / "Scripts" / "python.exe"
    if not build_py.is_file():
        die(f"缺编译解释器 {build_py} (需 Python 3.12 + Cython)")
    guard_port(17366)
    run([sys.executable, TOOLS / "compile_free_pyd.py",
         "--free-root", out_root, "--build-py", build_py], cwd=FREE_REPO)


def step_exe(out_root: Path, skip: bool) -> Path:
    log(f"\n[4/6] {STEP_LABELS['exe']}")
    exe = out_root / "lkwg-pvp-assistant.exe"
    if skip:
        if not exe.is_file():
            die("--skip-exe 但包内没有现成 exe")
        log("    --skip-exe: 复用包内现有 exe")
        return exe

    tauri = out_root / "src-tauri"
    if not (tauri / "Cargo.toml").is_file():
        die(f"缺 {tauri / 'Cargo.toml'}")
    cargo = shutil.which("cargo")
    if cargo is None:
        die("cargo 不在 PATH (需 Rust 工具链)")

    started = time.time()
    # 不设 CARGO_TARGET_DIR: 用树内 src-tauri/target, 由 sync_free 跨重建保留
    run([cargo, "build", "--release"], cwd=tauri)
    built = tauri / "target" / "release" / "lkwg-pvp-assistant.exe"
    if not built.is_file():
        die(f"cargo 未产出 exe: {built}")
    shutil.copy2(built, exe)
    log(f"    ✓ exe {exe.stat().st_size / 1e6:.0f} MB ({time.time() - started:.0f}s)")
    return exe


def verify_exe_port(exe: Path) -> None:
    data = exe.read_bytes()
    free_hits, paid_hits = data.count(b"17366"), data.count(b"17365")
    if free_hits == 0:
        die("exe 内不含 17366 (免费端口) — 可能编成了付费壳")
    if paid_hits:
        die(f"exe 内出现付费端口 17365 ({paid_hits} 处) — 免费包不能连付费端口")
    log(f"    ✓ 端口校验: 17366 ×{free_hits}, 无 17365")


def step_zip(out_root: Path, zip_path: Path) -> None:
    log(f"\n[5/6] 泄漏终检")
    leaks = []
    for path in out_root.rglob("*"):
        low = path.name.lower()
        if any(pat in low for pat in LEAK_PATTERNS):
            leaks.append(str(path.relative_to(out_root)))
    if leaks:
        for item in leaks[:10]:
            log(f"    ✗ {item}")
        die(f"发现 {len(leaks)} 个泄漏文件, 拒绝打包")

    # 源码树不应残留明文核心模块(只有 rkpp_live_tools.py 按设计保持明文入口)
    plain = [p for p in (out_root / "src").rglob("*.py")
             if p.stem in {"calc_damage", "calc_skill", "calc_panel", "calc_starfall",
                           "calc_enemy_stat", "damage_calculator", "seadata",
                           "rkpp_protocol", "rkpp_state", "rkpp_client", "tgcp"}]
    if plain:
        die(f"核心模块仍是明文: {[str(p.relative_to(out_root)) for p in plain[:5]]}")
    log("    ✓ 无源码/凭据/明文核心模块泄漏")

    log(f"\n[6/6] {STEP_LABELS['zip']}")
    for cache in out_root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in out_root.rglob("*.pyc"):
        try:
            pyc.unlink()
        except OSError:
            pass
    # src-tauri 是构建输入, 分发包只需要 exe
    if (out_root / "src-tauri").exists():
        kill_stragglers(out_root)
        for attempt in range(3):
            try:
                shutil.rmtree(out_root / "src-tauri")
                break
            except OSError:
                time.sleep(2)

    if zip_path.exists():
        zip_path.unlink()

    files = [p for p in out_root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    log(f"    压缩 {len(files)} 个文件 / {total / 1e6:.0f} MB …")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(files):
            zf.write(path, path.relative_to(out_root.parent))
    log(f"    ✓ {zip_path.name}  {zip_path.stat().st_size / 1e6:.0f} MB")


# ---------------------------------------------------------------- 主流程

def main() -> None:
    ap = argparse.ArgumentParser(description="免费版一键出包")
    ap.add_argument("--main", type=Path, default=DEFAULT_MAIN, help="主仓根目录")
    ap.add_argument("--rkpp", type=Path, default=DEFAULT_RKPP, help="解码器目录")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--nuget", type=Path,
                    default=DEFAULT_MAIN / "dist" / "python-3.12.10.nupkg")
    ap.add_argument("--wheelhouse", type=Path,
                    default=DEFAULT_MAIN / "dist" / "free-wheelhouse")
    ap.add_argument("--skip-exe", action="store_true",
                    help="复用包内现有 exe(只改 Python 侧时用, 省 4 分钟)")
    ap.add_argument("--from", dest="start", choices=STEPS, default="sync",
                    help="从指定阶段开始(断点续跑)")
    ap.add_argument("--clean", action="store_true",
                    help="先删掉输出目录再全量重建")
    args = ap.parse_args()

    main_root = args.main.resolve()
    rkpp = args.rkpp.resolve()
    out_root = args.out.resolve()
    zip_path = out_root.with_suffix(".zip")

    if not (main_root / "src").is_dir():
        die(f"主仓不存在或结构不对: {main_root}")
    if not rkpp.is_dir():
        die(f"解码器目录不存在: {rkpp}")

    start_idx = STEPS.index(args.start)
    log("=" * 60)
    log(f"免费版出包  →  {out_root.name}.zip")
    log(f"主仓 {main_root}")
    log(f"解码器 {rkpp}")
    log("=" * 60)

    if args.clean and start_idx == 0:
        log("\n    清空输出目录…")
        fresh_dir(out_root)

    started = time.time()
    if start_idx <= 0:
        step_sync(out_root, main_root, rkpp)
    if start_idx <= 2:
        step_python(out_root, args.nuget.resolve(), args.wheelhouse.resolve())
    if start_idx <= 3:
        step_cython(out_root, main_root)
    if start_idx <= 4:
        exe = step_exe(out_root, args.skip_exe)
    else:
        exe = out_root / "lkwg-pvp-assistant.exe"
    if not exe.is_file():
        die(f"缺 exe: {exe}")
    verify_exe_port(exe)
    if start_idx <= 5:
        step_zip(out_root, zip_path)

    log("\n" + "=" * 60)
    log(f"完成, 用时 {(time.time() - started) / 60:.1f} 分钟")
    log(f"  目录: {out_root}")
    log(f"  分发包: {zip_path}  ({zip_path.stat().st_size / 1e6:.0f} MB)")
    log("=" * 60)
    log("\n分发前提醒:")
    log("  · 解压后先跑 redist\\环境安装.bat (VC++ 运行库 + WebView2)")
    log("  · rkpp_clean 必须与 洛克小助手Free 同级, 否则实时识别起不来")
    log("  · 实机验证: 启动 / 窗口定位 / 实战识别 / 悬浮窗 / AI 军师")


if __name__ == "__main__":
    main()
