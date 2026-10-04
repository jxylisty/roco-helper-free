# -*- coding: utf-8 -*-
"""build_shiny_species.py — 从官方 API 缓存重建异色繁育物种库 shiny_species.json

数据源（全部为官方一手数据, 不再依赖过期的 egg_data/shiny_families 二级文件）:
  1. 官方列表缓存: crawler_official_api/output/cache/list.json
     每只精灵自带 has_shiny 标记 + egg_group_names 蛋组（含最新赛季, 无需二级源）
  2. 进化链: 主仓 src/pvp/data/pet_family.json（资源更新器自动维护;
     最新巨型 petId 可能缺链, 由 handbook_no 兜底归族）

聚合语义:
  has_shiny → pet_family 链归族 → 同 handbook_no 归族 → 同规范化名归族
  （并查集三级合并; 与主仓 shiny_family_builder.py 的家族语义对齐）
  家族蛋组 = 全体异色成员蛋组并集; 基础形态 = stage 最小者做代表行。
  无有效蛋组（未发现组/纯观赏）的家族不进繁育库。

产出（双写主仓与免费仓）:
  src/gui/web/assets/data/shiny_species.json
  dist_free/src/gui/web/assets/data/shiny_species.json （本仓内相对路径）

维护姿势: 游戏新赛季后重跑 crawler 的 update_data.py fetch 刷新 list.json,
再跑本脚本即可, 名单自动跟进, 无需改代码。
"""
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_ROOT = Path(r"d:\洛克王国ai\lkwgai_pvp_assistant")

CANDIDATE_LISTS = [
    Path(r"C:\Users\zzx05\Documents\HBuilderProjects\luokewangguo\crawler_official_api\output\cache\list.json"),
    MAIN_ROOT / "tools" / "output" / "cache" / "list.json",
]
PET_FAMILY_PATH = MAIN_ROOT / "src" / "pvp" / "data" / "pet_family.json"

OUT_PATHS = [
    MAIN_ROOT / "src" / "gui" / "web" / "assets" / "data" / "shiny_species.json",
    ROOT / "dist_free" / "src" / "gui" / "web" / "assets" / "data" / "shiny_species.json",
]

# 调研/拆包口径（2026-10 社区拆包 + 百蛋实测交叉验证, 官方未公示）:
# 子代外显异色蛋 0.72%/颗, 外显炫彩蛋 0.36%/颗 — 每蛋常量, 不随异色亲代数量线性相加。
# 单位: 百分点×100（centi-percent, 即 72 = 0.72%）
SHINY_EGG_RATE_CENTI = 72
IRIDESCENT_EGG_RATE_CENTI = 36

GROUP_ORDER = [
    "天空", "大地", "巨灵", "魔力", "软体", "妖精", "动物",
    "植物", "昆虫", "机械", "拟人", "海洋", "两栖", "巨龙"
]

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def norm(n):
    """去零宽字符 + 剥全角括号后缀(（首领形态）等)。"""
    return re.sub(r"（[^）]*）$", "", str(n or "").translate(_ZW).strip())


def clean_group(g):
    """官方组名 → 本地组名; 未发现/空返回 None。"""
    g = str(g or "").replace("组", "").replace("岩石", "大地").strip()
    return g if g and g != "未发现" else None


def load_pets():
    for p in CANDIDATE_LISTS:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit(f"官方列表缓存不存在: {CANDIDATE_LISTS}")


def build_species():
    pets = load_pets()
    pf = json.loads(PET_FAMILY_PATH.read_text(encoding="utf-8")) if PET_FAMILY_PATH.exists() else {}

    shiny = [p for p in pets if p.get("has_shiny")]
    if not shiny:
        raise SystemExit("缓存中无 has_shiny 精灵, 数据源异常")

    # ---- 并查集: pet_family 链 + 同 handbook_no + 同规范化名 ----
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for p in shiny:
        parent.setdefault("n:%s" % p["pet_id"], "n:%s" % p["pet_id"])
        fid = (pf.get(str(p["pet_id"])) or {}).get("family_id")
        if fid is not None:
            union("n:%s" % p["pet_id"], "f:%s" % fid)

    by_book = defaultdict(list)
    by_key = defaultdict(list)
    for p in shiny:
        by_book[str(p.get("handbook_no"))].append(p)
        by_key[norm(p["name"])].append(p)
    for grp in list(by_book.values()) + list(by_key.values()):
        for other in grp[1:]:
            union("n:%s" % grp[0]["pet_id"], "n:%s" % other["pet_id"])

    groups = defaultdict(list)
    for p in shiny:
        groups[find("n:%s" % p["pet_id"])].append(p)

    # ---- 组装: 蛋组并集为空(不可生蛋)的家族剔除 ----
    group_rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    species_list = []
    for members in groups.values():
        fam_groups = set()
        for m in members:
            for raw in m.get("egg_group_names") or []:
                g = clean_group(raw)
                if g:
                    fam_groups.add(g)
        if not fam_groups:
            continue

        names = sorted({norm(m["name"]) for m in members})
        base = sorted(members, key=lambda x: (x.get("stage") or 9, x["pet_id"]))[0]
        base_name = norm(base["name"])

        species_list.append({
            "name": base_name,
            "aliases": [n for n in names if n != base_name],
            "groups": sorted(fam_groups, key=lambda g: group_rank.get(g, 99)),
            "petId": base["pet_id"],
        })

    # 排序: 旧表习惯按 petId 升序, 保持稳定可 diff
    species_list.sort(key=lambda s: s["petId"])

    result_data = {
        "SPECIES": species_list,
        "GROUP_ORDER": GROUP_ORDER,
        "SHINY_EGG_RATE_CENTI": SHINY_EGG_RATE_CENTI,
        "IRIDESCENT_EGG_RATE_CENTI": IRIDESCENT_EGG_RATE_CENTI,
        "PET_ID": {s["name"]: s["petId"] for s in species_list},
        "STORAGE_KEY": "roco_shiny_planner_state_v1",
        "BUILT_FROM": {
            "list_cache": str(next(p for p in CANDIDATE_LISTS if p.exists())),
            "list_mtime": __import__("datetime").datetime.fromtimestamp(
                max(p.stat().st_mtime for p in CANDIDATE_LISTS if p.exists())
            ).strftime("%Y-%m-%d %H:%M"),
            "shiny_total": len(shiny),
            "breedable_families": len(species_list),
        },
    }

    written = []
    for out in OUT_PATHS:
        if out.parent.exists():
            out.write_text(json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(out)
            print(f"Written {len(species_list)} breedable families -> {out}")
    if not written:
        raise SystemExit("无任何输出路径存在, 检查 OUT_PATHS")

    # ---- 联动产出 shiny_breeding.json（eggquery 异色核心页签数据源, 此前为 0 字节空壳）----
    breeding = [
        {"name": s["name"], "aliases": s["aliases"], "groups": s["groups"], "petId": s["petId"]}
        for s in species_list
    ]
    for out in OUT_PATHS:
        breeding_path = out.parent / "shiny_breeding.json"
        if breeding_path.parent.exists():
            breeding_path.write_text(json.dumps(breeding, ensure_ascii=False, indent=1), encoding="utf-8")

    return result_data


if __name__ == "__main__":
    build_species()
