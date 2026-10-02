# -*- coding: utf-8 -*-
"""build_shiny_species.py — 构建最新的异色繁育核心物种库 shiny_species.json

数据源:
  1. 官方 API 缓存: crawler_official_api/output/cache/list.json (211 异色精灵)
  2. 蛋组库: src/gui/web/assets/data/egg_data.json (422 精灵蛋组)
  3. 异色家族库: src/gui/web/assets/data/shiny_families.json (88 异色家族)
  4. 现有核心库: src/gui/web/assets/data/shiny_species.json (旧版 32 核心)

产出:
  src/gui/web/assets/data/shiny_species.json (全量可繁育异色核心物种，含别名/蛋组/头像ID)
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_ROOT = Path(r"d:\洛克王国ai\lkwgai_pvp_assistant")

CRAWLER_LIST = Path(r"C:\Users\zzx05\Documents\HBuilderProjects\luokewangguo\crawler_official_api\output\cache\list.json")
EGG_DATA_PATH = MAIN_ROOT / "src" / "gui" / "web" / "assets" / "data" / "egg_data.json"
FAMILIES_PATH = MAIN_ROOT / "src" / "gui" / "web" / "assets" / "data" / "shiny_families.json"
OLD_SPECIES_PATH = MAIN_ROOT / "src" / "gui" / "web" / "assets" / "data" / "shiny_species.json"

OUT_PATHS = [
    MAIN_ROOT / "src" / "gui" / "web" / "assets" / "data" / "shiny_species.json",
    ROOT / "dist_free" / "src" / "gui" / "web" / "assets" / "data" / "shiny_species.json",
]


def norm(n):
    """去除括号后缀与空格"""
    return re.sub(r"（[^）]*）$", "", str(n or "")).strip()


def build_species():
    egg_data = json.loads(EGG_DATA_PATH.read_text(encoding="utf-8"))
    families = json.loads(FAMILIES_PATH.read_text(encoding="utf-8"))
    cur_species = json.loads(OLD_SPECIES_PATH.read_text(encoding="utf-8"))

    egg_by_id = {item.get("petId"): item for item in egg_data}
    egg_by_name = {item.get("name"): item for item in egg_data}

    # 蛋组标准顺序（保留原 12 蛋组并追加扩充组）
    # 洛克王国常见：植物、动物、天空、妖精、大地(岩石)、拟人、机械、昆虫、海洋、软体、魔力、巨灵、两栖、巨龙
    group_order = [
        "天空", "大地", "巨灵", "魔力", "软体", "妖精", "动物",
        "植物", "昆虫", "机械", "拟人", "海洋", "两栖", "巨龙"
    ]

    new_species_list = []
    seen_names = set()
    pet_id_map = {}

    # 第一轮：保留原有 32 核心精灵的代表名与习惯配置，扩充家族别名和蛋组
    for s in cur_species.get("SPECIES", []):
        s_name = s["name"]
        s_groups = list(s.get("groups", []))
        s_aliases = list(s.get("aliases", []))

        # 在 families 中查找对应家族
        fam = next((f for f in families if s_name in f["family"] or any(a in f["family"] for a in s_aliases)), None)
        if fam:
            for m in fam.get("members", []):
                nm = norm(m["name"])
                if nm != s_name and nm not in s_aliases:
                    s_aliases.append(nm)
                pid = int(m.get("conf_id", 0))
                e = egg_by_id.get(pid) or egg_by_name.get(m["name"]) or egg_by_name.get(nm)
                if e:
                    for g in e.get("eggGroups", []):
                        g_clean = g.replace("组", "").replace("岩石", "大地")
                        if g_clean != "未发现" and g_clean not in s_groups:
                            s_groups.append(g_clean)

        pid = cur_species.get("PET_ID", {}).get(s_name)
        if not pid and fam and len(fam["members"]) > 0:
            # 取该家族形态中 conf_id 最小或在 egg_data 中的 id
            sorted_m = sorted(fam["members"], key=lambda x: int(x.get("conf_id", 999999)))
            pid = int(sorted_m[0].get("conf_id", 0))

        if pid:
            pet_id_map[s_name] = pid

        entry = {
            "name": s_name,
            "aliases": s_aliases,
            "groups": s_groups,
            "petId": pid
        }
        new_species_list.append(entry)
        seen_names.add(s_name)
        for a in s_aliases:
            seen_names.add(a)

    # 第二轮：遍历 families 中尚未覆盖的所有家族，提取属于生蛋蛋组（可繁育）的家族
    for fam in families:
        covered = any(norm(m["name"]) in seen_names for m in fam.get("members", []))
        if covered:
            continue

        # 检查是否具备有效蛋组
        fam_groups = set()
        for m in fam.get("members", []):
            pid = int(m.get("conf_id", 0))
            nm = norm(m["name"])
            e = egg_by_id.get(pid) or egg_by_name.get(m["name"]) or egg_by_name.get(nm)
            if e:
                for g in e.get("eggGroups", []):
                    g_clean = g.replace("组", "").replace("岩石", "大地")
                    if g_clean != "未发现":
                        fam_groups.add(g_clean)

        if not fam_groups:
            # 不可生蛋/未发现组，跳过
            continue

        # 选择基础形态（按总种族值最低优先，同种族值按 conf_id 最低）
        sorted_members = sorted(
            fam.get("members", []),
            key=lambda x: (x.get("total") or 9999, int(x.get("conf_id", 999999)))
        )
        base_member = sorted_members[0]
        base_name = norm(base_member["name"])

        aliases = [norm(m["name"]) for m in fam.get("members", []) if norm(m["name"]) != base_name]
        aliases = list(dict.fromkeys(aliases))

        pid = int(base_member.get("conf_id", 0))
        if pid:
            pet_id_map[base_name] = pid

        entry = {
            "name": base_name,
            "aliases": aliases,
            "groups": sorted(list(fam_groups)),
            "petId": pid
        }
        new_species_list.append(entry)
        seen_names.add(base_name)
        for a in aliases:
            seen_names.add(a)

    result_data = {
        "SPECIES": new_species_list,
        "GROUP_ORDER": group_order,
        "SINGLE_SHINY_PERCENT_CENTI": 36,
        "DOUBLE_SHINY_PERCENT_CENTI": 72,
        "PET_ID": pet_id_map,
        "STORAGE_KEY": "roco_shiny_planner_state_v1",
    }

    # 写入目标路径
    for out in OUT_PATHS:
        if out.parent.exists():
            out.write_text(json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Written {len(new_species_list)} breedable species to {out}")

    return result_data


if __name__ == "__main__":
    build_species()
