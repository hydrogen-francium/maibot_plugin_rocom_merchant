"""洛克王国远行商人 MaiBot 插件主入口。

为了兼容 MaiBot 可能不实例化 plugin 的加载顺序，所有运行时状态都放在 core.py 的模块级单例里。
plugin.py 只负责：
1. 声明 config_schema（供 webui 生成 config.toml）
2. 通过 register_plugin 注册到 MaiBot
3. get_plugin_components 里把权限配置写到 Command 类属性
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Type

from src.plugin_system import (
    BasePlugin,
    ComponentInfo,
    ConfigField,
    get_logger,
    register_plugin,
)

from .components import (
    MerchantBroadcastCommand,
    MerchantListSubscriptionsCommand,
    MerchantQueryCommand,
    MerchantRewriteCommand,
    MerchantSubscribeCommand,
    MerchantUnsubscribeCommand,
    RocomBreedingCommand,
    RocomEggSearchCommand,
)
from .core import DEFAULT_BROADCAST_PROMPT

logger = get_logger("rocom_merchant")


@register_plugin
class RocomMerchantPlugin(BasePlugin):
    """洛克王国远行商人插件。"""

    plugin_name: str = "rocom_merchant_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["httpx", "jinja2", "playwright"]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "api": "后端 API 配置",
        "merchant": "远行商人订阅与播报配置",
        "llm_config": "播报重写使用的 LLM 模型",
        "permissions": "权限设置，定义哪些用户可以使用插件功能",
    }

    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="rocom_merchant_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "api": {
            "base_url": ConfigField(
                type=str,
                default="https://wegame.shallow.ink",
                description="API 基础地址",
            ),
            "wegame_api_key": ConfigField(
                type=str,
                default="",
                description="WeGame API Key",
                input_type="password",
                hint="必填；原项目 README 里给了一个公共测试 key",
            ),
            "timeout": ConfigField(
                type=int, default=15, description="HTTP 超时时间（秒）", min=3, max=60
            ),
        },
        "merchant": {
            "broadcast_enabled": ConfigField(
                type=bool,
                default=True,
                description="启用定时播报",
                hint="默认值；插件重载时生效。管理员可用 /远行商人播报 开|关 临时调整",
            ),
            "broadcast_interval": ConfigField(
                type=int,
                default=300,
                description="定时播报轮询间隔（秒）",
                min=30,
                max=3600,
                hint="默认 5 分钟一次，按轮次去重推送",
            ),
            "default_items": ConfigField(
                type=list,
                default=["国王球", "棱镜球", "炫彩精灵蛋"],
                description="默认订阅商品",
                item_type="string",
                hint="订阅命令不带商品参数时使用该列表",
            ),
            "render_timeout": ConfigField(
                type=int, default=30000, description="图片渲染超时（毫秒）", min=5000, max=120000
            ),
            "broadcast_prompt": ConfigField(
                type=str,
                input_type="textarea",
                default=DEFAULT_BROADCAST_PROMPT,
                description="播报文本重写的 Prompt 模板",
                hint="支持变量：{matched_items} {all_items} {round} {total} {countdown} {date}",
            ),
            "llm_rewrite_enabled": ConfigField(
                type=bool,
                default=True,
                description="启用 LLM 重写播报文本",
                hint="关闭后播报走固定模板；管理员可用 /远行商人重写 开|关 临时切换",
            ),
            "subscriptions": ConfigField(
                type=list,
                item_type="object",
                item_fields={
                    "stream_id": {
                        "type": "string",
                        "label": "Stream ID",
                        "placeholder": "群聊的 stream_id（可从 /远行商人订阅列表 获取）",
                    },
                    "group_id": {
                        "type": "string",
                        "label": "群号",
                        "placeholder": "仅作展示用",
                    },
                    "items": {
                        "type": "string",
                        "label": "监听商品",
                        "placeholder": "空格或逗号分隔，如：国王球 棱镜球 炫彩精灵蛋",
                    },
                    "description": {
                        "type": "string",
                        "label": "备注",
                        "placeholder": "群名或说明，可选",
                    },
                },
                default=[],
                description="群聊订阅列表",
                hint="命令订阅/取消会自动同步这里；手动添加后需重启插件，以 stream_id 为唯一标识",
            ),
        },
        "llm_config": {
            "llm_group": ConfigField(
                type=str,
                choices=["lpmm_entity_extract", "lpmm_rdf_build", "planner", "replyer", "tool_use", "utils", "vlm"],
                default="utils",
                description="播报重写使用的 LLM 模型分组",
                hint="在 llm_list 为空时生效。模型本身的参数从 webui “为模型分配功能” 中读取",
            ),
            "llm_list": ConfigField(
                type=list,
                item_type="string",
                default=[],
                description="播报重写使用的具体模型名（模型管理里添加的名字）",
                hint="非空时优先使用，此时下方 max_tokens / temperature 等生效",
            ),
            "max_tokens": ConfigField(
                type=int,
                default=512,
                description="最大输出 token（仅对 llm_list 生效）",
            ),
            "temperature": ConfigField(
                type=float,
                default=0.9,
                description="生成温度（仅对 llm_list 生效）",
            ),
            "slow_threshold": ConfigField(
                type=float,
                default=30,
                description="慢请求阈值（秒，仅对 llm_list 生效）",
            ),
            "selection_strategy": ConfigField(
                type=str,
                choices=["balance", "random"],
                default="balance",
                description="多模型选择策略（仅对 llm_list 生效）",
            ),
        },
        "permissions": {
            "admin_id_list": ConfigField(
                type=list,
                item_type="string",
                default=["1234567890"],
                description="管理员用户ID列表，可配置订阅、临时开关播报",
            ),
            "permission_mode": ConfigField(
                type=str,
                choices=["whitelist", "blacklist"],
                default="blacklist",
                description="权限模式：白名单仅列表内可用；黑名单列表内禁用",
            ),
            "user_id_list": ConfigField(
                type=list,
                item_type="object",
                item_fields={
                    "user_id": {
                        "type": "string",
                        "label": "用户的QQ号",
                        "placeholder": "用户的QQ号",
                    },
                    "description": {
                        "type": "string",
                        "label": "描述",
                        "placeholder": "可选描述，仅便于查看",
                    },
                },
                default=[{"user_id": "1234567890", "description": "示例用户"}],
                description="黑/白名单用户列表",
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        permissions: Dict[str, Any] = {}
        try:
            permissions = self.config.get("permissions", {}) or {}
        except Exception:
            pass

        permission_mode: str = permissions.get("permission_mode", "blacklist")
        if permission_mode not in {"whitelist", "blacklist"}:
            logger.warning(f"未知权限模式 {permission_mode}，降级为 blacklist")
            permission_mode = "blacklist"

        user_id_list_raw = permissions.get("user_id_list", []) or []
        user_id_list = [
            str(item["user_id"])
            for item in user_id_list_raw
            if isinstance(item, dict) and item.get("user_id")
        ]
        admin_id_list = [str(x) for x in (permissions.get("admin_id_list", []) or [])]

        for cmd in (
            MerchantQueryCommand,
            MerchantSubscribeCommand,
            MerchantUnsubscribeCommand,
            MerchantBroadcastCommand,
            MerchantRewriteCommand,
            MerchantListSubscriptionsCommand,
            RocomEggSearchCommand,
            RocomBreedingCommand,
        ):
            cmd.permission_mode = permission_mode
            cmd.user_id_list = user_id_list
            cmd.admin_id_list = admin_id_list

        return [
            (MerchantQueryCommand.get_command_info(), MerchantQueryCommand),
            (MerchantSubscribeCommand.get_command_info(), MerchantSubscribeCommand),
            (MerchantUnsubscribeCommand.get_command_info(), MerchantUnsubscribeCommand),
            (MerchantBroadcastCommand.get_command_info(), MerchantBroadcastCommand),
            (MerchantRewriteCommand.get_command_info(), MerchantRewriteCommand),
            (MerchantListSubscriptionsCommand.get_command_info(), MerchantListSubscriptionsCommand),
            (RocomEggSearchCommand.get_command_info(), RocomEggSearchCommand),
            (RocomBreedingCommand.get_command_info(), RocomBreedingCommand),
        ]
