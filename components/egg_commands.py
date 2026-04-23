"""洛克查蛋 / 洛克配种 命令。"""

from __future__ import annotations

import base64
import re
from typing import List, Optional, Tuple

from src.plugin_system import BaseCommand, get_logger
from src.plugin_system.apis import send_api

from ..core import get_client, get_egg_searcher
from ..egg_searcher import SearchResult
from ..renderer import MerchantRenderer
from .commands import _check_permission, _PermissionedCommand, _user_id_of

logger = get_logger("rocom_merchant.egg_commands")


# ===== 工具 =====

_NUM_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?$")


def _try_num(s: str) -> Optional[float]:
    if not s:
        return None
    if _NUM_PATTERN.match(s):
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _parse_egg_args(raw: str) -> Tuple[Optional[float], Optional[float], str]:
    """解析 /洛克查蛋 的参数，返回 (身高cm, 体重kg, 名称剩余部分)。

    支持：
    - 「25 1.5」        → 身高 25cm，体重 1.5kg
    - 「25」            → 仅身高
    - 「身高25 体重1.5」 → 明确关键字
    - 「h25 w1.5」
    - 「喵喵」          → 名称
    - 「身高25 喵喵」   → 身高 + 名称（少见，但允许）
    """
    height: Optional[float] = None
    weight: Optional[float] = None
    nums: List[float] = []
    name_parts: List[str] = []

    for tok in (raw or "").split():
        tl = tok.lower()
        if tl.startswith("身高") or tl.startswith("h"):
            v = _try_num(tok.lstrip("身高hH"))
            if v is not None:
                height = v
                continue
        if tl.startswith("体重") or tl.startswith("w"):
            v = _try_num(tok.lstrip("体重wW"))
            if v is not None:
                weight = v
                continue
        v = _try_num(tok)
        if v is not None:
            nums.append(v)
        else:
            name_parts.append(tok)

    if nums:
        if height is None and len(nums) >= 1:
            height = nums[0]
        if weight is None and len(nums) >= 2:
            weight = nums[1]

    return height, weight, " ".join(name_parts).strip()


_renderer: Optional[MerchantRenderer] = None


def _get_renderer(self: BaseCommand) -> MerchantRenderer:
    """懒初始化 renderer，但让 render_timeout 随 config 变化。"""
    global _renderer
    timeout = int(self.get_config("merchant.render_timeout", 30000) or 30000)
    if _renderer is None or _renderer.render_timeout != timeout:
        import os
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _renderer = MerchantRenderer(plugin_dir=plugin_dir, render_timeout=timeout)
    return _renderer


async def _send_image_bytes(command: BaseCommand, image_bytes: bytes) -> bool:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return await send_api.image_to_stream(
        image_base64=b64,
        stream_id=command.message.chat_stream.stream_id,
        storage_message=True,
    )


# ===== 洛克查蛋 =====

class RocomEggSearchCommand(_PermissionedCommand):
    """
    `/洛克查蛋 <精灵名|身高[体重]>`
    - `喵喵` 按名称查蛋组
    - `25`、`25 1.5`、`身高25 体重1.5` 按尺寸反查（有双参数时优先走后端 API）
    """

    command_name = "rocom_egg_search"
    command_description = "查精灵蛋组/尺寸反查"
    command_pattern = r"^[/#]洛克查蛋(?:\s+(?P<raw>.+))?\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")

        raw = (self.matched_groups.get("raw") if self.matched_groups else "") or ""
        raw = raw.strip()
        if not raw:
            await self.send_text(
                "用法：/洛克查蛋 <精灵名>\n"
                "     /洛克查蛋 25        （按身高反查）\n"
                "     /洛克查蛋 25 1.5    （身高+体重）\n"
                "     /洛克查蛋 身高25 体重1.5"
            )
            return True, "help", 1

        height, weight, name = _parse_egg_args(raw)
        searcher = get_egg_searcher()
        renderer = _get_renderer(self)

        # 身高/体重反查
        if height is not None or weight is not None:
            use_api = height is not None and weight is not None
            data = None
            text_result = None
            if use_api:
                api_res = await get_client().query_pet_size(height / 100, weight)
                if api_res is not None:
                    data = searcher.build_size_search_data_from_api(height, weight, api_res)
                    text_result = self._text_from_api(height, weight, api_res)
            if data is None:
                results = searcher.search_by_size(height=height, weight=weight)
                data = searcher.build_size_search_data(height, weight, results)
                text_result = searcher.build_size_search_text(height, weight, results)

            image_bytes = await renderer.render_html(
                "searcheggs/size.html",
                data,
                {"viewport_width": 1000, "viewport_height": 1200},
            )
            if image_bytes:
                await _send_image_bytes(self, image_bytes)
            else:
                await self.send_text(text_result or "（无结果）")
            return True, "size", 1

        # 名称查蛋
        sr = searcher.search(name)
        if sr.match_type == SearchResult.NOT_FOUND:
            await self.send_text(f"❌ 未找到名为「{name}」的精灵，请检查名称后重试。")
            return True, "not-found", 1
        if sr.match_type == SearchResult.MULTI:
            data = searcher.build_candidates_render_data(name, sr.candidates)
            image_bytes = await renderer.render_html(
                "searcheggs/candidates.html",
                data,
                {"viewport_width": 1000, "viewport_height": 900},
            )
            if image_bytes:
                await _send_image_bytes(self, image_bytes)
            else:
                await self.send_text(searcher.build_candidates_text(name, sr.candidates))
            return True, "multi", 1

        pet = sr.pet
        hint = ""
        if sr.match_type == SearchResult.FUZZY and pet is not None:
            zh = pet.get("localized", {}).get("zh", {}).get("name", "")
            hint = f"🔍 模糊匹配到「{zh}」"

        data = searcher.build_search_data(pet)
        data["commandHint"] = "💡 /洛克查蛋 <名称> | /洛克查蛋 身高25 体重1.5 | /洛克配种 <父> <母>"
        data["copyright"] = "MaiBot × WeGame Locke Kingdom Plugin"
        if hint:
            await self.send_text(hint)
        image_bytes = await renderer.render_html(
            "searcheggs/index.html",
            data,
            {"viewport_width": 1000, "viewport_height": 1600},
        )
        if image_bytes:
            await _send_image_bytes(self, image_bytes)
        else:
            await self.send_text(
                f"🥚 {data['pet_name']} (#{data['pet_id']})\n"
                f"属性：{data['type_label']}\n"
                f"蛋组：{data['egg_groups_label']}\n"
                f"可配种精灵数：{data['total_compatible']}"
            )
        return True, "ok", 1

    @staticmethod
    def _text_from_api(height, weight, api_res: dict) -> str:
        cond = []
        if height is not None:
            cond.append(f"身高={height}cm")
        if weight is not None:
            cond.append(f"体重={weight}kg")
        cond_str = " + ".join(cond)
        exact = api_res.get("exactResults") or []
        cand = api_res.get("candidates") or []
        if not exact and not cand:
            return f"❌ 后端反查：未找到符合 {cond_str} 的精灵。"
        lines: List[str] = []
        if exact:
            lines.append(f"✅ 完美匹配 {cond_str}（{len(exact)} 只）：")
            for i, it in enumerate(exact[:10], 1):
                lines.append(f"  {i}. {it.get('pet', '?')} (#{it.get('petId', '-')})")
        if cand:
            if lines:
                lines.append("")
            lines.append(f"🔍 候选 {cond_str}（{len(cand)} 只）：")
            for i, it in enumerate(cand[:10], 1):
                lines.append(f"  {i}. {it.get('pet', '?')} (#{it.get('petId', '-')})")
        return "\n".join(lines)


# ===== 洛克配种 =====

class RocomBreedingCommand(_PermissionedCommand):
    """
    `/洛克配种 <父> <母>` — 两精灵配种兼容性判定（前父后母，孵蛋结果跟随母体）
    `/洛克配种 <精灵名>`  — 想孵某精灵：列出合适父体
    """

    command_name = "rocom_breeding"
    command_description = "配种判定 / 想孵某精灵"
    command_pattern = r"^[/#]洛克配种(?:\s+(?P<raw>.+))?\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")

        raw = (self.matched_groups.get("raw") if self.matched_groups else "") or ""
        tokens = raw.strip().split()
        if not tokens:
            await self.send_text(
                "🥚 配种用法：\n"
                "  /洛克配种 <父体> <母体>   判断能否配种（孵蛋跟随母体）\n"
                "  /洛克配种 <精灵名>        查询想要该精灵需要哪些父母"
            )
            return True, "help", 1

        searcher = get_egg_searcher()
        renderer = _get_renderer(self)

        # 单参数：想孵某精灵
        if len(tokens) == 1:
            name = tokens[0]
            sr = searcher.search(name)
            if sr.match_type == SearchResult.NOT_FOUND:
                await self.send_text(f"❌ 未找到名为「{name}」的精灵。")
                return True, "not-found", 1
            if sr.match_type == SearchResult.MULTI:
                await self._render_candidates(name, sr.candidates, searcher, renderer)
                return True, "multi", 1
            data = searcher.build_want_pet_data(sr.pet)
            image_bytes = await renderer.render_html(
                "searcheggs/want.html",
                data,
                {"viewport_width": 1000, "viewport_height": 1400},
            )
            if image_bytes:
                await _send_image_bytes(self, image_bytes)
            else:
                await self.send_text(searcher.build_want_pet_text(sr.pet))
            return True, "want", 1

        # 双参数：父×母
        name_a = tokens[0]
        name_b = " ".join(tokens[1:])
        sr_a = searcher.search(name_a)
        if sr_a.match_type == SearchResult.NOT_FOUND:
            await self.send_text(f"❌ 未找到名为「{name_a}」的精灵。")
            return True, "not-found", 1
        if sr_a.match_type == SearchResult.MULTI:
            await self._render_candidates(name_a, sr_a.candidates, searcher, renderer)
            return True, "multi", 1

        sr_b = searcher.search(name_b)
        if sr_b.match_type == SearchResult.NOT_FOUND:
            await self.send_text(f"❌ 未找到名为「{name_b}」的精灵。")
            return True, "not-found", 1
        if sr_b.match_type == SearchResult.MULTI:
            await self._render_candidates(name_b, sr_b.candidates, searcher, renderer)
            return True, "multi", 1

        father, mother = sr_a.pet, sr_b.pet
        data = searcher.build_pair_data(mother, father)
        data["commandHint"] = "💡 默认前父后母，孵蛋结果跟随母体 | /洛克配种 <精灵名> 查怎么孵"
        data["copyright"] = "MaiBot × WeGame Locke Kingdom Plugin"
        image_bytes = await renderer.render_html(
            "searcheggs/pair.html",
            data,
            {"viewport_width": 1000, "viewport_height": 1000},
        )
        if image_bytes:
            await _send_image_bytes(self, image_bytes)
        else:
            ma, fa = data["mother"]["name"], data["father"]["name"]
            if data["compatible"]:
                shared = " / ".join(data["shared_egg_group_labels"])
                await self.send_text(
                    f"✅ 父体 {fa} × 母体 {ma} 可以配种！\n"
                    f"共享蛋组：{shared}\n"
                    f"孵出结果：{ma}（跟随母体）\n"
                    f"孵化时长：{data['hatch_label']}"
                )
            else:
                await self.send_text(
                    f"❌ {fa} × {ma} 无法配种。\n原因：{'；'.join(data['reasons'])}"
                )
        return True, "pair", 1

    async def _render_candidates(
        self, keyword: str, candidates: list, searcher, renderer: MerchantRenderer
    ) -> None:
        data = searcher.build_candidates_render_data(keyword, candidates)
        image_bytes = await renderer.render_html(
            "searcheggs/candidates.html",
            data,
            {"viewport_width": 1000, "viewport_height": 900},
        )
        if image_bytes:
            await _send_image_bytes(self, image_bytes)
        else:
            await self.send_text(searcher.build_candidates_text(keyword, candidates))
