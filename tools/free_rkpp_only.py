import ast
import json
import re
import textwrap
from pathlib import Path


def _definition(tree, name, kind=ast.FunctionDef):
    matches = [node for node in ast.walk(tree) if isinstance(node, kind) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one definition of {name}, found {len(matches)}")
    return matches[0]


def _rewrite(source, edits):
    lines = source.splitlines(keepends=True)
    for node, replacement in sorted(edits, key=lambda edit: edit[0].lineno, reverse=True):
        start = min([node.lineno, *(item.lineno for item in getattr(node, "decorator_list", ()))])
        lines[start - 1:node.end_lineno] = [replacement]
    result = "".join(lines)
    ast.parse(result)
    return result


def _block(source, first, last):
    return "".join(source.splitlines(keepends=True)[first - 1:last])


def sanitize_adviser_schema(source):
    start = source.index('    "ai_vision.json": {')
    end = source.index('    "ai_companion.json": {', start)
    section = source[start:end]
    section = re.sub(r'^\s*\{"key": "(?:enabled|base_url|api_key|model|prompt|interval_s|template)",[^\n]*\n',
                     "", section, flags=re.M)
    section = re.sub(r'(?m)^(\s*"desc": )"[^"\n]*"', r'\1"AI 军师只读战术建议配置"', section, count=1)
    section = re.sub(r'("desc": "[^"\n]*?)，默认为 [^"\n]+', r'\1，请填写自用服务配置', section)
    return source[:start] + section + source[end:]


def remove_vision_startup(source):
    tree = ast.parse(source)
    bridge = _definition(tree, "AppBridge", ast.ClassDef)
    init = _definition(bridge, "__init__")
    edits = []
    for node in init.body:
        if isinstance(node, ast.Assign) and any(
            ast.unparse(target) == "self.tool._frame_provider" for target in node.targets
        ):
            edits.append((node, ""))
        if isinstance(node, ast.Try):
            modules = {child.module for child in ast.walk(node) if isinstance(child, ast.ImportFrom)}
            if "src.tasks.daily_runner" in modules:
                edits.append((node, "        self.daily = None\n"))
            if "src.perception.ball_watcher" in modules:
                edits.append((node, "        self.ball_watcher = None\n"))
    anchor = "            frame_provider=self._live_capture_frame,\n"
    if len(edits) != 3 or source.count(anchor) != 1:
        raise ValueError("Free bridge vision startup anchors changed")
    return _rewrite(source, edits).replace(anchor, "")


def remove_season_report(root: Path):
    index = root / "src/gui/web/index.html"
    html = index.read_text(encoding="utf-8")
    for pattern in (
        r'^[ \t]*<button class="page-tab-btn" data-tab="history"[^\n]*\n',
        r'^[ \t]*<!-- ===== 页面: 赛季战报[^\n]*\n.*?^(?=[ \t]*</section>)',
        r'^[ \t]*<script src="assets/history\.js"></script>\n',
    ):
        html, count = re.subn(pattern, "", html, flags=re.M | re.S)
        if count != 1:
            raise ValueError(f"Season report HTML anchor matched {count} times")
    index.write_text(html.replace("；排位对战单独进入赛季战报", ""), encoding="utf-8")
    (root / "src/gui/web/assets/history.js").unlink()
    pages = root / "src/gui/web/assets/app_pages.js"
    source = pages.read_text(encoding="utf-8")
    pages.write_text(source.replace("history: ['pvp', 'history']", "history: ['pvp', 'battle']"),
                     encoding="utf-8")

    for relative, names in (
        ("src/gui/bridge.py", {"pvp_get_history", "pvp_get_history_stats", "pvp_get_match_replay",
                              "pvp_record_match", "pvp_clear_history", "pvp_generate_mock_history",
                              "pvp_history_cloud_pull"}),
        ("src/gui/local_api.py", {"handle_history", "handle_rounds", "handle_round_detail"}),
        ("src/server/app.py", {"handle_history_endpoint", "handle_rounds_endpoint", "handle_round_detail_endpoint"}),
    ):
        path = root / relative
        source = path.read_text(encoding="utf-8")
        edits = [(node, "") for node in ast.walk(ast.parse(source))
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        path.write_text(_rewrite(source, edits), encoding="utf-8")

    bridge = root / "src/gui/bridge_pvp_data.py"
    source = bridge.read_text(encoding="utf-8")
    anchors = ("from src.gui.bridge_pvp_history import PvpHistoryMixin\n", "PvpHistoryMixin, ")
    if any(source.count(anchor) != 1 for anchor in anchors):
        raise ValueError("PvpHistoryMixin anchor changed")
    for anchor in anchors:
        source = source.replace(anchor, "", 1)
    ast.parse(source)
    bridge.write_text(source, encoding="utf-8")
    (root / "src/gui/bridge_pvp_history.py").unlink()


def apply_rkpp_only(root: Path):
    species_path = root / "src/gui/web/assets/data/shiny_species.json"
    species = json.loads(species_path.read_text(encoding="utf-8"))
    species.pop("BUILT_FROM", None)
    species_path.write_text(json.dumps(species, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def edit(relative, transform):
        path = root / relative
        result = transform(path.read_text(encoding="utf-8"))
        if path.suffix == ".py":
            ast.parse(result)
        path.write_text(result, encoding="utf-8")

    def bridge(source):
        tree = ast.parse(source)
        edits = [(node, "") for node in tree.body if isinstance(node, ast.ImportFrom)
                 and node.module == "src.gui.bridge_vision"]
        api = _definition(tree, "Api", ast.ClassDef)
        edits.extend((node, "") for node in api.body if isinstance(node, ast.FunctionDef)
                     and (node.name.startswith(("vision_", "roi_")) or node.name == "ai_vision_test"))
        result = _rewrite(source, edits)
        if result.count("VisionMixin, ") != 1 or not edits:
            raise ValueError("VisionMixin anchor changed")
        return remove_vision_startup(result.replace("VisionMixin, ", ""))

    def pipeline(source):
        tree = ast.parse(source)
        result = _definition(tree, "PvpResult", ast.ClassDef)
        serializer = _definition(tree, "to_dict")
        return (
            "from dataclasses import dataclass, field\nfrom pathlib import Path\n\n"
            "PROJECT_ROOT = Path(__file__).resolve().parents[2]\n\n"
            + _block(source, result.decorator_list[0].lineno, result.end_lineno)
            + "\n\nclass PvpPipeline:\n    _cached_result = None\n\n"
            + _block(source, serializer.lineno, serializer.end_lineno)
            + "\n\n_pipeline = PvpPipeline()\n\ndef get_pipeline():\n    return _pipeline\n"
        )

    def source_loop(source):
        tree = ast.parse(source)
        loop = _definition(tree, "_pvp_loop")
        cycle = next(node for node in loop.body if isinstance(node, ast.While))
        guarded = next(node for node in cycle.body if isinstance(node, ast.Try))
        rkpp = next(node for node in guarded.body if isinstance(node, ast.If)
                    and ast.unparse(node.test) == "source == 'rkpp'")
        body = textwrap.indent(textwrap.dedent(_block(
            source, rkpp.body[0].lineno, rkpp.body[-1].end_lineno)), " " * 16)
        replacement = (
            "    def _pvp_loop(self, gen: int = 0):\n"
            "        import time as _time\n"
            "        from src.pvp.pvp_pipeline import get_pipeline\n\n"
            "        pipeline = get_pipeline()\n"
            "        pipeline._cached_result = None\n"
            "        self._pipeline = pipeline\n"
            "        latency_battle_id = 0\n"
            "        latency_names_logged = False\n"
            + _block(source, cycle.lineno, guarded.lineno)
            + body
            + _block(source, guarded.handlers[0].lineno, loop.end_lineno)
        )
        return _rewrite(source, [(loop, replacement),
                                 (_definition(tree, "_watch_loop"), ""),
                                 (_definition(tree, "_start_watch_loop"), "")])

    def window(source):
        tree = ast.parse(source)
        names = {"capture", "_capture_printwindow", "_capture_bitblt", "_is_valid_frame"}
        edits = [(node, "") for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in names]
        edits.extend((node, "") for node in tree.body if isinstance(node, ast.Import)
                     and any(alias.name in {"cv2", "numpy", "win32ui"} for alias in node.names))
        return _rewrite(source, edits)

    def health(source):
        tree = ast.parse(source)
        result = _rewrite(source, [(_definition(tree, "_probe_ocr"), "")])
        result = result.replace("            self._probe_ocr(),\n", "")
        return re.sub(r'^\s*\("(?:cv2|numpy|rapidocr_onnxruntime|mss)",[^\n]*\n',
                      "", result, flags=re.M)

    def settings(source):
        tree = ast.parse(source)
        getter = '''    def ai_vision_get_settings(self) -> dict:
        from src.gui.ai_decision import DEFAULTS, load_config

        config = load_config()
        return {"success": True, "settings": {key: config[key] for key in DEFAULTS} | {"pvp_source": "rkpp"}}
'''
        setter = '''    def ai_vision_save_settings(self, params: dict) -> dict:
        from src.gui.ai_decision import CONFIG_FILE, DEFAULTS, load_config

        if not isinstance(params, dict) or not params:
            return {"success": False, "message": "参数为空"}
        config = load_config()
        config.update({key: params[key] for key in DEFAULTS if key in params})
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.ai_vision_get_settings()
'''
        return _rewrite(source, [(_definition(tree, "ai_vision_get_settings"), getter),
                                 (_definition(tree, "ai_vision_save_settings"), setter),
                                 (_definition(tree, "ai_vision_test"), "")])

    def decision_defaults(source):
        tree = ast.parse(source)
        defaults = next(node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "DEFAULTS"
                                for target in node.targets))
        values = ast.literal_eval(defaults.value)
        for key in ("ai_decision_base_url", "ai_decision_api_key", "ai_decision_model"):
            values[key] = ""
        return _rewrite(source, [(tree.body[0], '"""只读对战军师。"""\n'),
                                 (defaults, f"DEFAULTS = {values!r}\n")])

    edit("src/gui/bridge.py", bridge)
    edit("src/gui/bridge_common.py", sanitize_adviser_schema)
    edit("src/gui/bridge_settings.py", settings)
    edit("src/gui/ai_decision.py", decision_defaults)
    edit("src/gui/bridge_pvp_source.py", source_loop)
    edit("src/pvp/pvp_pipeline.py", pipeline)
    edit("src/capture/window_capture.py", window)
    edit("src/gui/bridge_health.py", health)
    edit("src/gui/bridge_widget.py", lambda source: re.sub(
        r"^[ \t]*self\._start_watch_loop\(\)[^\n]*\n", "", source, flags=re.M))
    edit("src/gui/web/index.html", lambda source: re.sub(
        r'^[ \t]*<div[^\n]*>\n[ \t]*<span[^\n]*>视觉印记辅助[^\n]*\n'
        r'[^<]*(?:<label[^>]*>.*?</label>)\s*</div>\n', "", source, flags=re.M | re.S))
    edit("src-tauri/src/main.rs", lambda source: source.replace(
        '    let build_venv = root.join(".venv-build")',
        '    let bundled_python = root.join("python").join("python.exe");\n'
        '    if bundled_python.is_file() {\n        candidates.push(bundled_python);\n    }\n'
        '    let build_venv = root.join(".venv-build")', 1))
    for relative in ("src/gui/bridge_vision.py", "src/pvp/lineup_recognizer.py"):
        (root / relative).unlink()
