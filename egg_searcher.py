"""洛克王国查蛋引擎（自包含）。

合并了原插件的 `eggs.py` + `egg_service.py`：
- 搜索（精确/模糊/多候选）
- 身高体重反查（本地匹配 + 后端 API 响应解析）
- 配种兼容性判定
- 渲染数据构建（模板用）
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from src.plugin_system import get_logger

logger = get_logger("rocom_merchant.eggs")

# ===== 蛋组元数据 =====

EGG_GROUP_META = {
    1:  {"label": "未发现", "desc": "不能和任何精灵生蛋，多用于传说中的精灵"},
    2:  {"label": "怪兽",   "desc": "像怪兽一样，或者比较野性的动物"},
    3:  {"label": "两栖",   "desc": "两栖动物和水边生活的多栖动物"},
    4:  {"label": "虫",     "desc": "看起来像虫子的精灵"},
    5:  {"label": "飞行",   "desc": "会飞的精灵"},
    6:  {"label": "陆上",   "desc": "生活在陆地上的精灵"},
    7:  {"label": "妖精",   "desc": "可爱的小动物，以及神话中的精灵"},
    8:  {"label": "植物",   "desc": "看起来像植物的精灵"},
    9:  {"label": "人型",   "desc": "看起来像人的精灵"},
    10: {"label": "软体",   "desc": "看起来软软的精灵，圆形多为软体动物"},
    11: {"label": "矿物",   "desc": "身体由矿物组成的精灵"},
    12: {"label": "不定形", "desc": "没有固定形态的精灵，包括水、火、灵魂、能量"},
    13: {"label": "鱼",     "desc": "看起来像鱼的精灵"},
    14: {"label": "龙",     "desc": "看起来像龙的精灵"},
    15: {"label": "机械",   "desc": "身体由机械组成的精灵"},
}


def get_egg_group_label(group_id: int) -> str:
    meta = EGG_GROUP_META.get(group_id)
    return meta["label"] if meta else f"蛋组{group_id}"


def format_egg_groups(group_ids: List[int]) -> str:
    if not group_ids:
        return "暂无蛋组数据"
    return " / ".join(get_egg_group_label(gid) for gid in group_ids)


# ===== 搜索结果类型 =====

class SearchResult:
    EXACT = "exact"
    FUZZY = "fuzzy"
    MULTI = "multi"
    NOT_FOUND = "not_found"

    def __init__(self, match_type: str, pet: Optional[Dict] = None, candidates: Optional[List[Dict]] = None):
        self.match_type = match_type
        self.pet = pet
        self.candidates: List[Dict] = candidates or []


# ===== 查蛋引擎 =====

class EggSearcher:
    """蛋组查询引擎 + 渲染数据构建。"""

    def __init__(self, data_file: str):
        self._data_file = data_file
        self._pets: List[Dict] = []
        self._by_id: Dict[int, Dict] = {}
        self._by_zh: Dict[str, Dict] = {}
        self._by_en: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._data_file):
            logger.error(f"Pets.json 不存在: {self._data_file}")
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._pets = raw if isinstance(raw, list) else []
            for p in self._pets:
                self._by_id[p["id"]] = p
                zh = p.get("localized", {}).get("zh", {}).get("name", "")
                if zh:
                    self._by_zh[zh] = p
                en = p.get("name", "").lower()
                if en:
                    self._by_en[en] = p
            logger.info(f"已加载 {len(self._pets)} 只精灵")
        except Exception as e:
            logger.error(f"加载 Pets.json 失败: {e}")

    # ---- 基础搜索 ----

    def search(self, keyword: str) -> SearchResult:
        kw = (keyword or "").strip()
        if not kw:
            return SearchResult(SearchResult.NOT_FOUND)
        if kw in self._by_zh:
            return SearchResult(SearchResult.EXACT, pet=self._by_zh[kw])
        try:
            pid = int(kw)
            if pid in self._by_id:
                return SearchResult(SearchResult.EXACT, pet=self._by_id[pid])
        except ValueError:
            pass
        if kw.lower() in self._by_en:
            return SearchResult(SearchResult.EXACT, pet=self._by_en[kw.lower()])

        kw_lower = kw.lower()
        hits: List[Dict] = []
        for p in self._pets:
            zh = p.get("localized", {}).get("zh", {}).get("name", "")
            en = p.get("name", "")
            if kw_lower in zh.lower() or kw_lower in en.lower():
                hits.append(p)
        if len(hits) == 1:
            return SearchResult(SearchResult.FUZZY, pet=hits[0])
        if len(hits) > 1:
            return SearchResult(SearchResult.MULTI, candidates=hits[:20])
        return SearchResult(SearchResult.NOT_FOUND)

    # ---- 身高 / 体重反查 ----

    def search_by_size(self, height: Optional[float] = None, weight: Optional[float] = None) -> Dict[str, List[Dict]]:
        perfect: List[Dict] = []
        ranged: List[Dict] = []
        for p in self._pets:
            br = p.get("breeding") or {}
            h_lo, h_hi = br.get("height_low"), br.get("height_high")
            w_lo, w_hi = br.get("weight_low"), br.get("weight_high")

            h_match = None
            w_match = None
            if height is not None:
                if h_lo is not None and h_hi is not None:
                    if h_lo <= height <= h_hi:
                        h_match = "perfect"
                    elif h_lo * 0.85 <= height <= h_hi * 1.15:
                        h_match = "range"
                    else:
                        h_match = "none"
                else:
                    h_match = "none"
            if weight is not None:
                if w_lo is not None and w_hi is not None:
                    w_kg_lo, w_kg_hi = w_lo / 1000, w_hi / 1000
                    if w_kg_lo <= weight <= w_kg_hi:
                        w_match = "perfect"
                    elif w_kg_lo * 0.85 <= weight <= w_kg_hi * 1.15:
                        w_match = "range"
                    else:
                        w_match = "none"
                else:
                    w_match = "none"

            if height is not None and weight is not None:
                if h_match == "perfect" and w_match == "perfect":
                    perfect.append(p)
                elif h_match != "none" and w_match != "none":
                    ranged.append(p)
            elif height is not None:
                if h_match == "perfect":
                    perfect.append(p)
                elif h_match == "range":
                    ranged.append(p)
            elif weight is not None:
                if w_match == "perfect":
                    perfect.append(p)
                elif w_match == "range":
                    ranged.append(p)
        return {"perfect": perfect[:20], "range": ranged[:20]}

    # ---- 配种 ----

    def get_egg_groups(self, pet: Dict) -> List[int]:
        bp = pet.get("breeding_profile")
        return bp.get("egg_groups", []) if bp else []

    def get_compatible_pets(self, pet: Dict) -> List[Dict]:
        groups = set(self.get_egg_groups(pet))
        if not groups or 1 in groups:
            return []
        out = []
        for o in self._pets:
            if o["id"] == pet["id"]:
                continue
            og = set(self.get_egg_groups(o))
            if not og or 1 in og:
                continue
            if groups & og:
                out.append(o)
        return out

    def get_breeding_parents(self, pet: Dict) -> List[Dict]:
        """想要孵出指定精灵，可选的父体集合。"""
        return self.get_compatible_pets(pet)

    def evaluate_pair(self, a: Dict, b: Dict) -> Dict:
        ga, gb = set(self.get_egg_groups(a)), set(self.get_egg_groups(b))
        shared = sorted(ga & gb)
        reasons = []
        if not ga:
            reasons.append(f"{self._name(a)} 暂无蛋组数据")
        if not gb:
            reasons.append(f"{self._name(b)} 暂无蛋组数据")
        if 1 in ga:
            reasons.append(f"{self._name(a)} 属于「未发现」蛋组")
        if 1 in gb:
            reasons.append(f"{self._name(b)} 属于「未发现」蛋组")
        if not shared and not reasons:
            reasons.append("蛋组不相同，无法配种")
        br = a.get("breeding") or {}
        return {
            "compatible": not reasons and len(shared) > 0,
            "reasons": reasons,
            "shared_egg_groups": shared,
            "shared_egg_group_labels": [get_egg_group_label(g) for g in shared],
            "hatch_label": self._fmt_dur(br.get("hatch_data")),
            "weight_label": self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg"),
            "height_label": self._fmt_range(br.get("height_low"), br.get("height_high"), "cm"),
        }

    # ---- 渲染数据构建 ----

    def build_search_data(self, pet: Dict) -> Dict[str, Any]:
        egs = self.get_egg_groups(pet)
        compat = self.get_compatible_pets(pet)
        gmap: Dict[int, List[Dict]] = {gid: [] for gid in egs if gid != 1}
        for c in compat:
            for gid in egs:
                if gid in self.get_egg_groups(c) and gid != 1:
                    gmap.setdefault(gid, []).append(c)
        sections = []
        for gid in egs:
            meta = EGG_GROUP_META.get(gid, {})
            members = gmap.get(gid, [])
            sections.append({
                "id": gid,
                "label": meta.get("label", f"蛋组{gid}"),
                "desc": meta.get("desc", ""),
                "count": len(members),
                "members": [
                    {
                        "name": self._name(m),
                        "id": m["id"],
                        "type_label": self._type(m),
                        "egg_groups_label": format_egg_groups(self.get_egg_groups(m)),
                    }
                    for m in members[:30]
                ],
                "has_more": len(members) > 30,
                "total": len(members),
            })
        br = pet.get("breeding") or {}
        bp = pet.get("breeding_profile") or {}

        return {
            "pet_name": self._name(pet),
            "pet_id": pet["id"],
            "pet_icon": self._pet_icon_url(pet["id"]),
            "pet_image": self._pet_image_url(pet["id"]),
            "type_label": self._type(pet),
            "egg_groups_label": format_egg_groups(egs),
            "egg_groups": egs,
            "egg_group_labels": {gid: get_egg_group_label(gid) for gid in egs},
            "male_rate": bp.get("male_rate"),
            "female_rate": bp.get("female_rate"),
            "hatch_label": self._fmt_dur(br.get("hatch_data")),
            "weight_label": self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg"),
            "height_label": self._fmt_range(br.get("height_low"), br.get("height_high"), "cm"),
            "total_compatible": len(compat),
            "is_undiscovered": 1 in egs,
            "egg_group_sections": sections,
            "total_stats": sum(
                pet.get(k, 0)
                for k in ["base_hp", "base_phy_atk", "base_mag_atk", "base_phy_def", "base_mag_def", "base_spd"]
            ),
            "egg_details": self._build_egg_details(br),
        }

    def _build_egg_details(self, breeding: Dict) -> Dict[str, Any]:
        if not breeding:
            return {"has_data": False}

        def _prob(arr):
            if arr and isinstance(arr, list) and len(arr) == 2:
                return f"{arr[0]}/{arr[1]}", (arr[0] / arr[1] * 100 if arr[1] else None)
            return "暂无数据", None

        base_str, base_pct = _prob(breeding.get("egg_base_glass_prob_array"))
        add_str, add_pct = _prob(breeding.get("egg_add_glass_prob_array"))

        variants = breeding.get("variants") or []
        variant_list = []
        for v in variants:
            v_base = v.get("egg_base_glass_prob_array")
            variant_list.append({
                "id": v.get("id"),
                "name": v.get("name", ""),
                "hatch_label": self._fmt_dur(v.get("hatch_data")),
                "weight_label": self._fmt_range(self._wt(v.get("weight_low")), self._wt(v.get("weight_high")), "kg"),
                "height_label": self._fmt_range(v.get("height_low"), v.get("height_high"), "cm"),
                "precious_egg_type": v.get("precious_egg_type"),
                "precious_egg_label": self._get_precious_egg_label(v.get("precious_egg_type")),
                "base_prob_str": (
                    f"{v_base[0]}/{v_base[1]}" if v_base and isinstance(v_base, list) and len(v_base) == 2 else "暂无"
                ),
            })

        return {
            "has_data": True,
            "base_prob_str": base_str,
            "base_prob_pct": base_pct,
            "add_prob_str": add_str,
            "add_prob_pct": add_pct,
            "is_contact_add_glass": breeding.get("is_contact_add_glass_prob"),
            "is_contact_add_shining": breeding.get("is_contact_add_shining_prob"),
            "precious_egg_type": breeding.get("precious_egg_type"),
            "precious_egg_label": self._get_precious_egg_label(breeding.get("precious_egg_type")),
            "variants": variant_list,
            "variant_count": len(variant_list),
        }

    @staticmethod
    def _get_precious_egg_label(egg_type) -> str:
        if egg_type is None:
            return "普通蛋"
        m = {1: "迪莫蛋", 2: "星辰蛋", 3: "彩虹蛋", 4: "梦幻蛋", 5: "传说蛋", 6: "神秘蛋", 7: "特殊蛋"}
        return m.get(egg_type, f"珍稀蛋(类型{egg_type})")

    def build_pair_data(self, mother: Dict, father: Dict) -> Dict[str, Any]:
        ev = self.evaluate_pair(mother, father)
        return {
            "mother": {
                "name": self._name(mother),
                "id": mother["id"],
                "type_label": self._type(mother),
                "egg_groups_label": format_egg_groups(self.get_egg_groups(mother)),
            },
            "father": {
                "name": self._name(father),
                "id": father["id"],
                "type_label": self._type(father),
                "egg_groups_label": format_egg_groups(self.get_egg_groups(father)),
            },
            **ev,
        }

    def build_candidates_render_data(self, keyword: str, candidates: List[Dict]) -> Dict[str, Any]:
        return {
            "keyword": keyword,
            "count": len(candidates),
            "candidates": [self._format_pet_card(p) for p in candidates],
            "commandHint": "💡 请使用更精确的名称重新查询",
            "copyright": "MaiBot × WeGame Locke Kingdom Plugin",
        }

    def build_want_pet_data(self, pet: Dict) -> Dict[str, Any]:
        fathers = self.get_breeding_parents(pet)
        bp = pet.get("breeding_profile") or {}
        egg_groups = self.get_egg_groups(pet)
        return {
            "target": self._format_pet_card(pet),
            "egg_groups_label": format_egg_groups(egg_groups),
            "female_rate": bp.get("female_rate"),
            "male_rate": bp.get("male_rate"),
            "is_undiscovered": 1 in egg_groups,
            "fathers": [self._format_pet_card(p) for p in fathers[:30]],
            "father_count": len(fathers),
            "commandHint": "💡 /洛克配种 <父体> <母体> 查看详细结果",
            "copyright": "MaiBot × WeGame Locke Kingdom Plugin",
        }

    def build_size_search_data(
        self, height: Optional[float], weight: Optional[float], results: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height} cm")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        perfect = [self._format_pet_card(p) for p in (results or {}).get("perfect", [])]
        ranged = [self._format_pet_card(p) for p in (results or {}).get("range", [])]
        return {
            "query_label": " / ".join(conditions) if conditions else "尺寸反查",
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": "💡 /洛克查蛋 <精灵名> | /洛克查蛋 身高25 体重1.5",
            "copyright": "MaiBot × WeGame Locke Kingdom Plugin",
        }

    def build_size_search_data_from_api(
        self, height: Optional[float], weight: Optional[float], results: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        conditions = []
        if height is not None:
            conditions.append(f"身高 {height} cm")
        if weight is not None:
            conditions.append(f"体重 {weight} kg")
        perfect = [self._format_size_api_card(it) for it in (results or {}).get("exactResults") or []]
        ranged = [self._format_size_api_card(it) for it in (results or {}).get("candidates") or []]
        search_mode = (results or {}).get("searchMode") or ""
        subtitle = " / ".join(conditions) if conditions else "尺寸反查"
        if search_mode:
            subtitle = f"{subtitle} · 模式 {search_mode}"
        return {
            "query_label": subtitle,
            "perfect_matches": perfect,
            "range_matches": ranged,
            "total_count": len(perfect) + len(ranged),
            "has_results": bool(perfect or ranged),
            "commandHint": "💡 /洛克查蛋 <精灵名> | /洛克查蛋 身高25 体重1.5",
            "copyright": "MaiBot × WeGame Locke Kingdom Plugin",
        }

    # ---- 文本降级 ----

    def build_size_search_text(
        self, height: Optional[float], weight: Optional[float], results: Dict[str, List[Dict]]
    ) -> str:
        cond = []
        if height is not None:
            cond.append(f"身高={height}cm")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond) if cond else "当前条件"

        if not results or (not results["perfect"] and not results["range"]):
            return f"❌ 未找到符合 {cond_str} 的精灵。"

        lines: List[str] = []
        if results["perfect"]:
            lines.append(f"✅ 完美匹配 {cond_str}（共 {len(results['perfect'])} 只）：")
            for i, p in enumerate(results["perfect"][:10], 1):
                lines.append(f"  {i}. {self._line_for_pet(p)}")
            if len(results["perfect"]) > 10:
                lines.append(f"  ... 还有 {len(results['perfect']) - 10} 个")
        if results["range"]:
            if lines:
                lines.append("")
            lines.append(f"🔍 范围匹配 {cond_str}（±15%，共 {len(results['range'])} 只）：")
            for i, p in enumerate(results["range"][:10], 1):
                lines.append(f"  {i}. {self._line_for_pet(p)}")
            if len(results["range"]) > 10:
                lines.append(f"  ... 还有 {len(results['range']) - 10} 个")
        lines.append("\n💡 /洛克查蛋 <精灵名> 查看详细蛋组")
        return "\n".join(lines)

    def build_candidates_text(self, keyword: str, candidates: List[Dict]) -> str:
        lines = [f"🔍 「{keyword}」匹配到 {len(candidates)} 只精灵，请精确输入："]
        for i, p in enumerate(candidates[:10], 1):
            lines.append(f"  {i}. {self._line_for_pet(p)}")
        if len(candidates) > 10:
            lines.append(f"  ... 还有 {len(candidates) - 10} 个")
        lines.append("\n💡 请使用精确名称重新查询")
        return "\n".join(lines)

    def build_want_pet_text(self, pet: Dict) -> str:
        zh = self._name(pet)
        egs = self.get_egg_groups(pet)
        bp = pet.get("breeding_profile") or {}
        female_rate = bp.get("female_rate")

        lines = [f"🥚 想要孵出「{zh}」：", f"蛋组：{format_egg_groups(egs)}"]
        if 1 in egs:
            lines.append("⚠️ 该精灵属于「未发现」蛋组，无法通过配种获得。")
            return "\n".join(lines)
        lines.append(f"\n📌 母体必须是「{zh}」（孵蛋结果跟随母体）")
        if female_rate is not None:
            lines.append(f"   母体概率：{female_rate}%")
        fathers = self.get_breeding_parents(pet)
        if fathers:
            lines.append(f"\n🔗 可选父体（共 {len(fathers)} 只，需雄性）：")
            for i, f in enumerate(fathers[:15], 1):
                fm = (f.get("breeding_profile") or {}).get("male_rate")
                mh = f" (♂{fm}%)" if fm is not None else ""
                lines.append(f"  {i}. {self._name(f)}{mh} — {format_egg_groups(self.get_egg_groups(f))}")
            if len(fathers) > 15:
                lines.append(f"  ... 还有 {len(fathers) - 15} 只")
        lines.append("\n💡 /洛克配种 <父体> <母体> 查看详细配种结果")
        return "\n".join(lines)

    # ---- 私有工具 ----

    def _line_for_pet(self, p: Dict) -> str:
        zh = self._name(p)
        br = p.get("breeding") or {}
        h_str = self._fmt_range(br.get("height_low"), br.get("height_high"), "cm")
        w_str = self._fmt_range(self._wt(br.get("weight_low")), self._wt(br.get("weight_high")), "kg")
        egs = format_egg_groups(self.get_egg_groups(p))
        return f"{zh} (#{p['id']}) — {h_str} / {w_str} · {egs}"

    def _format_pet_card(self, pet: Dict) -> Dict[str, Any]:
        breeding = pet.get("breeding") or {}
        return {
            "id": pet["id"],
            "name": self._name(pet),
            "icon": self._pet_icon_url(pet["id"]),
            "image": self._pet_image_url(pet["id"]),
            "type_label": self._type(pet),
            "egg_groups_label": format_egg_groups(self.get_egg_groups(pet)),
            "height_label": self._fmt_range(breeding.get("height_low"), breeding.get("height_high"), "cm"),
            "weight_label": self._fmt_range(
                self._wt(breeding.get("weight_low")), self._wt(breeding.get("weight_high")), "kg"
            ),
        }

    def _format_size_api_card(self, item: Dict[str, Any]) -> Dict[str, Any]:
        pet_name = item.get("pet") or "未知精灵"
        pet_id = item.get("petId") or "-"
        probability = item.get("probability")
        match_count = item.get("matchCount")
        extras = []
        if probability is not None:
            extras.append(f"匹配概率 {probability}%")
        if match_count is not None:
            extras.append(f"命中次数 {match_count}")
        return {
            "id": pet_id,
            "name": pet_name,
            "icon": item.get("petIcon") or self._pet_icon_url(pet_id),
            "image": item.get("petImage") or self._pet_image_url(pet_id),
            "type_label": "后端未提供",
            "egg_groups_label": " / ".join(extras) if extras else "后端未提供",
            "height_label": self._fmt_range(item.get("diameterMin"), item.get("diameterMax"), "m"),
            "weight_label": self._fmt_range(item.get("weightMin"), item.get("weightMax"), "kg"),
        }

    @staticmethod
    def _name(p: Dict) -> str:
        return p.get("localized", {}).get("zh", {}).get("name", p.get("name", "???"))

    @staticmethod
    def _type(p: Dict) -> str:
        parts = []
        mt = p.get("main_type", {}).get("localized", {}).get("zh", "")
        if mt:
            parts.append(mt)
        st = (p.get("sub_type") or {}).get("localized", {}).get("zh", "")
        if st:
            parts.append(st)
        return " / ".join(parts) or "未知"

    @staticmethod
    def _fmt_dur(s) -> str:
        if not s or s <= 0:
            return "暂无数据"
        if s % 86400 == 0:
            return f"{s // 86400} 天"
        h = s / 3600
        return f"{int(h)} 小时" if h == int(h) else f"{h:.1f} 小时"

    @staticmethod
    def _wt(v) -> Optional[float]:
        return round(v / 1000, 1) if v is not None else None

    @staticmethod
    def _fmt_range(lo, hi, u: str) -> str:
        if lo is None and hi is None:
            return "暂无数据"
        if lo is not None and hi is not None:
            return f"{lo}{u}" if lo == hi else f"{lo}-{hi}{u}"
        return f"{lo or hi}{u}"

    @staticmethod
    def _asset_pet_id(pet_id) -> Optional[int]:
        try:
            n = int(pet_id)
        except (TypeError, ValueError):
            return None
        return n if n >= 3000 else n + 3000

    def _pet_icon_url(self, pet_id) -> str:
        a = self._asset_pet_id(pet_id)
        if a is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{a}/icon.png"

    def _pet_image_url(self, pet_id) -> str:
        a = self._asset_pet_id(pet_id)
        if a is None:
            return "{{_res_path}}img/roco_icon.png"
        return f"https://game.gtimg.cn/images/rocom/rocodata/jingling/{a}/image.png"


__all__ = ["EggSearcher", "SearchResult", "EGG_GROUP_META", "format_egg_groups", "get_egg_group_label"]
