"""远行商人插件的 Command 组件（自包含，不依赖 plugin 实例）。"""

from __future__ import annotations

import base64
from typing import List, Optional, Tuple

from src.plugin_system import BaseCommand, get_logger
from src.plugin_system.apis import send_api

from ..core import (
    current_round,
    ensure_broadcast_task,
    get_cached_merchant_image,
    get_client,
    get_store,
    get_broadcast_runtime_enabled,
    get_llm_rewrite_runtime_enabled,
    parse_products,
    persist_subscriptions_to_config,
    set_broadcast_runtime_enabled,
    set_config_getter,
    set_llm_rewrite_runtime_enabled,
)

logger = get_logger("rocom_merchant.commands")


def _user_id_of(command: BaseCommand) -> str:
    try:
        return str(command.message.message_info.user_info.user_id)
    except Exception:
        return ""


def _check_permission(user_id: str, mode: str, user_list: List[str]) -> bool:
    is_in = user_id in user_list
    if mode == "whitelist":
        return is_in
    if mode == "blacklist":
        return not is_in
    return True


class _PermissionedCommand(BaseCommand):
    """带权限校验的 Command 基类；每次 execute 把配置 getter 注入 core。"""

    permission_mode: str = "blacklist"
    user_id_list: List[str] = []
    admin_id_list: List[str] = []

    def _is_admin(self, user_id: str) -> bool:
        return user_id in self.admin_id_list

    def _sync_config_and_task(self) -> None:
        """把 self.get_config 注册给 core，然后确保播报任务已启动。"""
        set_config_getter(self.get_config)
        ensure_broadcast_task()

    async def _deny(self, reason: str) -> Tuple[bool, Optional[str], int]:
        await self.send_text(reason)
        return True, reason, 1


class MerchantQueryCommand(_PermissionedCommand):
    """查询当前远行商人商品：先发文字（含实时倒计时），再发缓存图片。"""

    command_name = "merchant_query"
    command_description = "查询当前轮次远行商人商品"
    command_pattern = r"^[/#]远行商人\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")

        res = await get_client().fetch(refresh=True)
        activity, products = parse_products(res)
        round_info = current_round()

        if not products:
            await self.send_text("当前远行商人暂无可用商品。")
            return True, "empty", 1

        # 先发文字：包含实时倒计时，用户立刻能看到
        names = "、".join(p["name"] for p in products)
        text = (
            f"🛒 远行商人 · 第 {round_info['current'] or '-'}/{round_info['total']} 轮"
            f"  剩余 {round_info['countdown']}\n"
            f"📦 商品：{names}"
        )
        await self.send_text(text)

        # 再发图片（命中缓存时几百毫秒；首次冷启动会慢一点）
        image_bytes = await get_cached_merchant_image(activity, products, round_info)
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            await send_api.image_to_stream(
                image_base64=b64,
                stream_id=self.message.chat_stream.stream_id,
                storage_message=True,
            )
        return True, "ok", 1


class MerchantSubscribeCommand(_PermissionedCommand):
    """订阅远行商人推送（仅管理员）。"""

    command_name = "merchant_subscribe"
    command_description = "订阅远行商人推送"
    command_pattern = r"^[/#]订阅远行商人(?:\s+(?P<items>.+))?\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")
        if not self._is_admin(user_id):
            return await self._deny("仅管理员可以配置远行商人订阅。")

        chat_stream = self.message.chat_stream
        stream_id = getattr(chat_stream, "stream_id", "") or ""
        group_info = getattr(chat_stream, "group_info", None)
        group_id = str(getattr(group_info, "group_id", "") or "") if group_info else ""

        if not stream_id or not group_info:
            return await self._deny("该命令仅支持群聊使用。")

        raw_items = (self.matched_groups.get("items") or "").strip() if self.matched_groups else ""
        custom_items: Optional[List[str]] = None
        if raw_items:
            custom_items = [t for t in raw_items.split() if t]

        default_items: List[str] = self.get_config(
            "merchant.default_items", ["国王球", "棱镜球", "炫彩精灵蛋"]
        ) or []
        selected = custom_items if custom_items else list(default_items)

        sub = {
            "group_id": group_id,
            "stream_id": stream_id,
            "items": selected,
            "last_push_round": "",
        }
        await get_store().upsert(stream_id, sub)
        await persist_subscriptions_to_config()

        source = "自定义商品" if custom_items else "默认商品"
        await self.send_text(
            f"已订阅远行商人推送（{source}）。\n"
            f"监听：{'、'.join(selected) or '（列表为空，将无法命中）'}\n"
            f"用法：/订阅远行商人 国王球 棱镜球；/取消订阅远行商人 取消订阅。"
        )
        return True, "ok", 1


class MerchantUnsubscribeCommand(_PermissionedCommand):
    """取消本群远行商人订阅（仅管理员）。"""

    command_name = "merchant_unsubscribe"
    command_description = "取消远行商人推送"
    command_pattern = r"^[/#]取消订阅远行商人\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")
        if not self._is_admin(user_id):
            return await self._deny("仅管理员可以取消远行商人订阅。")

        chat_stream = self.message.chat_stream
        stream_id = getattr(chat_stream, "stream_id", "") or ""
        group_info = getattr(chat_stream, "group_info", None)
        if not stream_id or not group_info:
            return await self._deny("该命令仅支持群聊使用。")

        deleted = await get_store().delete(stream_id)
        if deleted:
            await persist_subscriptions_to_config()
        await self.send_text("已取消本群远行商人订阅。" if deleted else "本群当前没有远行商人订阅。")
        return True, "ok", 1


class MerchantBroadcastCommand(_PermissionedCommand):
    """临时打开/关闭定时播报（仅管理员；插件重载后恢复成配置值）。"""

    command_name = "merchant_broadcast"
    command_description = "临时开关远行商人定时播报"
    command_pattern = r"^[/#]远行商人播报(?:\s+(?P<action>\S+))?\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")
        if not self._is_admin(user_id):
            return await self._deny("仅管理员可以调整播报开关。")

        action = (self.matched_groups.get("action") if self.matched_groups else "") or ""
        action = action.strip().lower()

        config_enabled = bool(self.get_config("merchant.broadcast_enabled", True))
        if action in {"开", "on", "enable", "1", "true"}:
            set_broadcast_runtime_enabled(True)
            state = "已开启（临时，插件重载后恢复配置）"
        elif action in {"关", "off", "disable", "0", "false"}:
            set_broadcast_runtime_enabled(False)
            state = "已关闭（临时，插件重载后恢复配置）"
        elif action in {"", "状态", "status"}:
            state = "当前开启" if get_broadcast_runtime_enabled() else "当前关闭"
        else:
            return await self._deny("用法：/远行商人播报 开|关|状态")

        await self.send_text(
            f"远行商人播报：{state}\n配置默认：{'开启' if config_enabled else '关闭'}"
        )
        return True, "ok", 1


class MerchantRewriteCommand(_PermissionedCommand):
    """临时切换播报是否走 LLM 重写（仅管理员；插件重载后恢复成配置值）。"""

    command_name = "merchant_rewrite"
    command_description = "临时开关播报 LLM 重写"
    command_pattern = r"^[/#]远行商人重写(?:\s+(?P<action>\S+))?\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")
        if not self._is_admin(user_id):
            return await self._deny("仅管理员可以调整重写开关。")

        action = (self.matched_groups.get("action") if self.matched_groups else "") or ""
        action = action.strip().lower()

        config_enabled = bool(self.get_config("merchant.llm_rewrite_enabled", True))
        if action in {"开", "on", "enable", "1", "true"}:
            set_llm_rewrite_runtime_enabled(True)
            state = "已开启（临时，插件重载后恢复配置）"
        elif action in {"关", "off", "disable", "0", "false"}:
            set_llm_rewrite_runtime_enabled(False)
            state = "已关闭（临时，插件重载后恢复配置）"
        elif action in {"", "状态", "status"}:
            state = "当前开启" if get_llm_rewrite_runtime_enabled() else "当前关闭"
        else:
            return await self._deny("用法：/远行商人重写 开|关|状态")

        await self.send_text(
            f"播报 LLM 重写：{state}\n配置默认：{'开启' if config_enabled else '关闭'}\n"
            f"关闭后播报使用固定模板文字。"
        )
        return True, "ok", 1


class MerchantListSubscriptionsCommand(_PermissionedCommand):
    """查看当前所有远行商人订阅（仅管理员）。"""

    command_name = "merchant_list_subscriptions"
    command_description = "查看远行商人订阅列表"
    command_pattern = r"^[/#]远行商人订阅列表\s*$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        self._sync_config_and_task()
        user_id = _user_id_of(self)
        if not _check_permission(user_id, self.permission_mode, self.user_id_list):
            return await self._deny(f"用户 {user_id} 没有使用该命令的权限。")
        if not self._is_admin(user_id):
            return await self._deny("仅管理员可以查看订阅列表。")

        all_subs = await get_store().all()
        if not all_subs:
            await self.send_text("当前没有任何群订阅远行商人。")
            return True, "empty", 1

        default_items: List[str] = self.get_config("merchant.default_items", []) or []
        lines: List[str] = [f"📋 远行商人订阅列表（{len(all_subs)} 个群）"]
        for idx, (key, sub) in enumerate(all_subs.items(), 1):
            group_id = sub.get("group_id") or "-"
            stream_id = sub.get("stream_id") or key
            items = sub.get("items") or []
            last = sub.get("last_push_round") or "（未推送）"
            items_str = "、".join(items) if items else "（空）"
            is_default = items == default_items
            lines.append(
                f"\n{idx}. 群 {group_id}"
                f"\n   stream_id: {stream_id}"
                f"\n   监听: {items_str}{'（默认配置）' if is_default else ''}"
                f"\n   上次推送轮次: {last}"
            )
        await self.send_text("\n".join(lines))
        return True, "ok", 1
