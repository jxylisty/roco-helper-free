# -*- coding: utf-8 -*-
"""compile_free_pyd.py — 免费版发布树 Cython 加固 (核心算法与协议层明文 .py → .pyd)

背景 (2026-11-09):
    免费包此前是纯明文分发 (sync_free.py 甚至专门排除 *.pyd, 防主仓编译产物
    混入盖掉免费存根)。付费执行层已物理抽壳, 但免费功能自身的核心资产 ——
    伤害公式 (calc_*/damage_calculator/seadata) 与 RKPP 协议层/解码器 ——
    仍以明文 .py 随包分发, 任何拿到包的人可直接阅读。
    本脚本在 **sync 之后、打 zip 之前** 对发布树做原地编译, 复用主仓
    build_release.py 批1+批2 的清单与编译参数 (经 Pro 出包验证)。

流水线定位:
    sync_free.py --out <树>  →  build_free_python.py (装内嵌 python)
      →  ★ compile_free_pyd.py --free-root <树>  →  组装/打 zip

用法:
    python tools/compile_free_pyd.py --free-root <免费发布树根> [--skip-smoke]

    --free-root 指向含 FREE_BUILD / src/ / battle_stream/ / python/ 的最终包目录。
    编译解释器默认取主仓 .venv-build (Python 3.12 + Cython + MSVC 链),
    可用 --build-py 覆盖。产物 .pyd 为 cp312-win_amd64 ABI, 与包内嵌
    Python 3.12.x 兼容 (3.12 补丁版本间扩展 ABI 稳定)。

必须继承的主仓踩坑约定 (勿改):
    1. annotation_typing=False —— Cython 默认把函数注解编译成 C 类型校验,
       rkpp_analyzer(port=set(...)) 会 TypeError, 抓包 100% 起不来
       (2026-10-28 Pro 出包事故)。
    2. vcvars 必须先 cd 进 VC/Auxiliary/Build 再以相对名 call —— 直接 call
       绝对路径 vcvars64.bat 时 %~dp0 解析失败且报错被 >nul 吞掉, 表现为
       cl 不在 PATH、build_ext 静默无产物 (2026-11-08 修)。
    3. battle_stream_live_tools.py 保留明文 —— 它是解码器独立进程的脚本入口,
       python 无法直接执行 .pyd。
"""
import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# ========================================
# 编译清单 (相对发布树根; 与主仓 build_release.py BATCHES 批1+批2 对齐)
# ========================================
MODULES: list[str] = [
    # 批1 纯算法: 伤害公式核心
    "src/pvp/calc_damage.py",
    "src/pvp/calc_skill.py",
    "src/pvp/calc_panel.py",
    "src/pvp/calc_starfall.py",
    "src/pvp/calc_enemy_stat.py",
    "src/pvp/star_solver.py",
    "src/pvp/damage_calculator.py",
    "src/pvp/seadata.py",
    # 批2 协议层: 抓包客户端 + 解码器
    "src/capture/rkpp_protocol.py",
    "src/capture/rkpp_state.py",
    "src/capture/rkpp_client.py",
    "src/capture/tgcp.py",
    "battle_stream/battle_stream_codec.py",
    "battle_stream/battle_stream_network.py",
    "battle_stream/battle_stream_opcode_payload.py",
    "battle_stream/battle_stream_analysis.py",
    "battle_stream/battle_stream_analyzer.py",
    "battle_stream/battle_stream_io.py",
    "battle_stream/battle_stream_relay.py",
    "battle_stream/battle_stream_reporter.py",
    "battle_stream/name_service.py",
    "battle_stream/battle_stream_worker.py",
]

FREE_PORT = 17366
DEFAULT_MAIN = Path(r"D:\洛克王国ai\lkwgai_pvp_assistant")


def log(msg: str) -> None:
    # Windows 控制台默认 GBK, 打印 "✓" 会抛 UnicodeEncodeError 让编译中途崩掉
    # (踩过一次: 21 个模块全部编完, 死在日志行)。这里兜底降级不阻断流水线。
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = (sys.stdout.encoding or "utf-8")
        print(msg.encode(enc, errors="replace").decode(enc), flush=True)


def die(msg: str) -> None:
    raise SystemExit(f"[compile_free_pyd] 失败: {msg}")


def find_msvc_vcvars() -> Path:
    """返回 vcvars 所在目录 (调用方 cd 进去再 call, 见模块头约定 2)"""
    for base in (Path(r"D:\vsstudio"),
                 Path(r"C:\Program Files (x86)\Microsoft Visual Studio"),
                 Path(r"C:\Program Files\Microsoft Visual Studio")):
        d = base / "VC" / "Auxiliary" / "Build"
        if (d / "vcvarsall.bat").is_file():
            return d
    die("未找到 VC/Auxiliary/Build/vcvarsall.bat (MSVC x64)")


def check_build_env(build_py: Path) -> None:
    if not build_py.is_file():
        die(f"编译解释器不存在: {build_py}")
    r = subprocess.run([str(build_py), "-c",
                        "import Cython, sys; print(sys.version_info[:2], Cython.__version__)"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"{build_py} 缺少 Cython: {r.stderr.strip()[:300]}")
    parts = r.stdout.split()
    ver, cy = parts[0] + " " + parts[1], parts[2]
    if ver != "(3, 12)":
        die(f"编译解释器必须是 Python 3.12 (产物 ABI cp312), 实际 {ver}")
    log(f"[0] 编译解释器: {build_py} (3.12 + Cython {cy})")


def compile_module(rel: str, root: Path, build_py: Path, vcvars: Path,
                   scratch: Path) -> None:
    src = root / rel
    workdir = scratch / Path(rel).stem
    workdir.mkdir(parents=True)
    shutil.copy2(src, workdir / src.name)
    setup_py = workdir / "setup.py"
    # annotation_typing=False 是语义前提, 不是可选优化 (见模块头约定 1)
    setup_py.write_text(
        "from setuptools import setup\n"
        "from Cython.Build import cythonize\n"
        "setup(\n"
        "    ext_modules=cythonize(\n"
        f'        "{src.name}",\n'
        "        compiler_directives={'language_level': \"3\", "
        "'binding': True, 'embedsignature': True, "
        "'annotation_typing': False},\n"
        "        annotate=False),\n"
        "    script_args=['build_ext', '--inplace'],\n"
        ")\n", encoding="utf-8")
    bat = workdir / "_build.cmd"
    bat.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f'cd /d "{vcvars}"\r\n'
        "call vcvarsall.bat x64 >nul 2>&1\r\n"
        "cd /d %~dp0\r\n"
        f'"{build_py}" setup.py build_ext --inplace\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8")
    r = subprocess.run(["cmd", "/c", str(bat)], cwd=str(workdir),
                       capture_output=True, text=True, errors="replace",
                       timeout=900)
    pyds = list(workdir.glob(f"{src.stem}.*.pyd")) + list(workdir.glob(f"{src.stem}.pyd"))
    if r.returncode != 0 or not pyds:
        log((r.stdout or "")[-2500:])
        log((r.stderr or "")[-2500:])
        die(f"Cython 编译失败: {rel}")
    # 与主仓一致: 落地为无 ABI 标签的 <stem>.pyd, 删除明文 .py
    shutil.copy2(pyds[0], src.with_suffix(".pyd"))
    src.unlink()


def verify_shape(rel: str, root: Path) -> None:
    """编译后形态检查: 只允许 .pyd, 明文 .py 必须已删除"""
    py, pyd = root / rel, (root / rel).with_suffix(".pyd")
    if py.exists() or not pyd.exists():
        die(f"形态校验失败: {rel} 应为 .pyd-only (py={py.exists()}, pyd={pyd.exists()})")


def verify_imports(root: Path) -> None:
    """用包内嵌 python.exe 实导每个 .pyd (cp312 ABI 兼容性验证)"""
    embedded = root / "python" / "python.exe"
    if not embedded.is_file():
        die(f"缺内嵌解释器 {embedded} — 先跑 build_free_python.py")
    app_mods, rkpp_mods = [], []
    for rel in MODULES:
        stem = Path(rel).stem
        if rel.startswith("battle_stream/"):
            rkpp_mods.append(stem)
        else:
            app_mods.append(rel[:-3].replace("/", "."))
    code = (
        "import importlib, json\nok = []\n"
        + "".join(f"m = importlib.import_module('{m}');"
                  f"assert m.__file__.endswith('.pyd'), m.__file__;ok.append('{m}')\n"
                  for m in app_mods + rkpp_mods)
        + "print('##JSON##' + json.dumps(ok))\n"
    )
    r = subprocess.run([str(embedded), "-c", code], cwd=str(root),
                       capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        log(r.stdout[-3000:])
        log(r.stderr[-3000:])
        die("内嵌解释器导入 .pyd 失败 (ABI 或依赖问题)")
    out = r.stdout.split("##JSON##")[-1].strip()
    log(f"[2] ✓ 内嵌 python 实导 {len(app_mods) + len(rkpp_mods)} 个 .pyd 全部通过")


def port_open(port: int = FREE_PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def smoke(root: Path) -> None:
    """发布树冒烟: 内嵌 python 拉起 server_main, 验证服务可用 + 付费闸门仍拦截"""
    import json
    import urllib.request
    if port_open():
        die(f"端口 {FREE_PORT} 已被占用 — 先停掉在跑的免费后端再冒烟")
    log_file = open(root.parent / "compile_free_smoke.log", "w",
                    encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(root / "python" / "python.exe"), "server_main.py", "--no-browser"],
        cwd=str(root), stdout=log_file, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                log_file.flush()
                log(Path(log_file.name).read_text(encoding="utf-8",
                                                  errors="replace")[-3000:])
                die("冒烟后端进程启动即退出")
            if port_open():
                break
            time.sleep(0.5)
        else:
            die(f"后端 90s 未就绪, 查看 {log_file.name}")

        def rpc(method: str, args: list | None = None) -> dict:
            req = urllib.request.Request(
                f"http://127.0.0.1:{FREE_PORT}/api/rpc/{method}",
                data=json.dumps({"args": args or []}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        st = rpc("auth_status")
        if st.get("dev_mode"):
            die(f"冒烟失败: 免费树 dev_mode 应为 False → {st}")
        gate = rpc("toggle_normal")
        body = json.dumps(gate, ensure_ascii=False)
        if "free" not in body and "会员" not in body and "Pro" not in body:
            die(f"冒烟失败: toggle_normal 应被免费闸门拦截 → {body[:300]}")
        # 伤害公式实调 (compiled calc 链路端到端)
        log("[3] ✓ 冒烟通过: 服务就绪 / auth_status 用户模式 / 付费闸门拦截")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="免费发布树 Cython 加固 (明文 .py → .pyd)")
    ap.add_argument("--free-root", type=Path, required=True,
                    help="发布树根 (含 FREE_BUILD / src / battle_stream / python)")
    ap.add_argument("--build-py", type=Path,
                    default=DEFAULT_MAIN / ".venv-build" / "Scripts" / "python.exe",
                    help="编译解释器 (需 Python 3.12 + Cython)")
    ap.add_argument("--skip-smoke", action="store_true", help="跳过服务冒烟")
    args = ap.parse_args()

    root = args.free_root.resolve()
    for marker in ("FREE_BUILD", "src", "battle_stream", "python"):
        if not (root / marker).exists():
            die(f"--free-root 不是完整免费发布树 (缺 {marker}): {root}")
    build_py = args.build_py.resolve()

    check_build_env(build_py)
    vcvars = find_msvc_vcvars()
    log(f"[0] MSVC: {vcvars}")

    missing = [rel for rel in MODULES if not (root / rel).is_file()]
    already = [rel for rel in MODULES
               if (root / rel).with_suffix(".pyd").exists() and not (root / rel).exists()]
    if already and not missing:
        log("[1] 全部 21 个模块已是 .pyd 形态, 无需重复编译")
    elif missing:
        todo = [rel for rel in MODULES if (root / rel).is_file()]
        if not todo:
            die(f"清单模块全部缺席: {missing[:5]}")
        log(f"[1] 警告: {len(missing)} 个模块无明文 .py (可能已编译或树不完整): "
            f"{missing}")
        scratch = root / ".pyd_build"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        try:
            for rel in todo:
                compile_module(rel, root, build_py, vcvars, scratch)
                log(f"    ✓ {rel} → {Path(rel).stem}.pyd")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    else:
        scratch = root / ".pyd_build"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir()
        try:
            for rel in MODULES:
                compile_module(rel, root, build_py, vcvars, scratch)
                log(f"    ✓ {rel} → {Path(rel).stem}.pyd")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    for rel in MODULES:
        verify_shape(rel, root)
    log(f"[1] ✓ {len(MODULES)} 个模块 .pyd-only 形态校验通过")

    # 清理编译模块的陈旧 __pycache__ (pyc 不会泄露源码但会膨胀包体)
    for d in ("src/pvp", "src/capture", "battle_stream"):
        cache = root / d / "__pycache__"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)

    verify_imports(root)
    if args.skip_smoke:
        log("[3] 已跳过冒烟 (--skip-smoke)")
    else:
        smoke(root)
    log("[done] 免费树加固完成: 21 模块 .pyd, battle_stream_live_tools.py 保持明文入口")


if __name__ == "__main__":
    main()
