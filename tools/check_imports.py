# -*- coding: utf-8 -*-
"""check_imports.py — 免费树导入图体检

扫描"将进入免费版的文件"(按 sync_free.py 排除清单), 用 AST 找出它们对
被排除模块(src.pvp / src.capture / 授权体系 / 付费 service)的引用,
区分【顶层导入=启动即炸, 必须处理】与【懒导入=函数内, 触发才炸】。

用法:
    python tools/check_imports.py [主仓路径] [报告输出路径]
报告默认写到 tools/_import_report.txt
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_free import EXCLUDE_DIRS, EXCLUDE_FILES, iter_source_files  # noqa: E402

MAIN = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"D:\洛克王国ai\lkwgai_pvp_assistant")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "_import_report.txt"

# 被排除(或将以存根形式存在)的模块前缀
# v0.4 拆分线: src.pvp / src.capture / bridge_pvp* / bridge_daily / server services
# 均已转免费整体带入, 不再算排除项; 真正缺席的只有付费执行层与授权体系。
EXCLUDED_PREFIXES = (
    "src.gui.auth", "auth_core",
    "src.driver.human_input",
    "src.perception.bag_scanner", "src.perception.ball_watcher",
    "src.gui.ai_autopilot",
    "src.tasks",
)
# from src.gui import XXX 形式需要单独核对的模块名
GUI_NAMES = {"auth", "ai_autopilot"}


def imported_excluded(node: ast.AST):
    """返回该 import 语句引用的被排除模块列表"""
    mods = []
    if isinstance(node, ast.Import):
        mods = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        mods = [node.module]
        if node.module == "src.gui":
            mods += [f"src.gui.{a.name}" for a in node.names if a.name in GUI_NAMES]
    return [m for m in mods
            if any(m == p or m.startswith(p + ".") for p in EXCLUDED_PREFIXES)]


def main() -> None:
    top, lazy, syntax_err = [], [], []
    for rel, src_path in iter_source_files(MAIN):
        if rel.suffix != ".py":
            continue
        try:
            tree = ast.parse(src_path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            syntax_err.append(f"{rel.as_posix()}: {e}")
            continue
        for node in ast.walk(tree):
            for m in imported_excluded(node):
                hit = {"file": rel.as_posix(), "line": node.lineno, "module": m}
                (top if node.col_offset == 0 else lazy).append(hit)

    # 顺带列出两个付费 service 的公开方法(存根签名用)
    svc_methods = {}
    for svc in ("src/server/services/pvp_service.py",
                "src/server/services/daily_service.py"):
        p = MAIN / svc
        names = []
        if p.exists():
            try:
                for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and not node.name.startswith("_"):
                        names.append(node.name)
            except SyntaxError:
                names = ["<语法错误>"]
        svc_methods[svc] = names

    with OUT.open("w", encoding="utf-8") as f:
        f.write(f"扫描主仓: {MAIN}\n")
        f.write(f"顶层导入(危险, 启动即炸): {len(top)} 处\n")
        f.write(f"懒导入(函数内, 触发才炸): {len(lazy)} 处\n")
        f.write(f"语法错误: {len(syntax_err)}\n\n")

        f.write("== 顶层导入(必须存根/修补) ==\n")
        for h in top:
            f.write(f"  {h['file']}:{h['line']}  import {h['module']}\n")
        f.write("\n== 懒导入(可接受, 触发时返回 Pro 提示即可) ==\n")
        for h in lazy:
            f.write(f"  {h['file']}:{h['line']}  import {h['module']}\n")
        if syntax_err:
            f.write("\n== 语法错误 ==\n")
            for m in syntax_err:
                f.write(f"  {m}\n")
        f.write("\n== 付费 service 公开方法(存根签名参考) ==\n")
        for svc, names in svc_methods.items():
            f.write(f"  {svc}: {', '.join(names) or '(无)'}\n")
    print(f"report -> {OUT}  top={len(top)} lazy={len(lazy)} err={len(syntax_err)}")


if __name__ == "__main__":
    main()
