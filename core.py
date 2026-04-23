"""远行商人核心模块：HTTP 客户端、订阅存储、渲染器、播报任务的模块级单例。

设计要点：
- 不依赖 plugin 实例。MaiBot 的插件加载顺序可能不会实例化 BasePlugin，但 Command 是独立注册的。
- Command 每次执行时通过 BaseCommand.get_config 读配置，把 getter 或关键值写入本模块；后台播报任务据此运转。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional

import httpx

try:
    import tomllib  # py 3.11+
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

try:
    import tomli_w
except Exception:  # pragma: no cover
    tomli_w = None  # type: ignore[assignment]

from src.plugin_system import get_logger, llm_api
from src.plugin_system.apis import send_api

try:
    from src.config.api_ada_configs import TaskConfig
except Exception:  # pragma: no cover
    TaskConfig = None  # type: ignore[assignment]

from .renderer import MerchantRenderer

logger = get_logger("rocom_merchant.core")

API_PATH = "/api/v1/games/rocom/merchant/info"

DEFAULT_BROADCAST_PROMPT = (
    "你是洛克王国的群聊播报小助手，请用 1-3 句自然、活泼的中文把下面的远行商人信息告诉群友。"
    "要像在群里说话一样口语化，不要用列表、标题、表情包堆砌，也不要复述原始字段名。"
    "可以提醒一下命中了他们订阅的商品，让他们去看看。\n\n"
    "本轮命中的订阅商品：{matched_items}\n"
    "当前轮次：第 {round} / {total} 轮\n"
    "剩余时间：{countdown}\n"
    "日期：{date}\n"
    "本轮全部商品：{all_items}"
)


# ===== HTTP 客户端 =====

class MerchantClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def fetch(self, refresh: bool = True) -> Optional[Dict[str, Any]]:
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        params = {"refresh": "true" if refresh else "false"}
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}{API_PATH}", headers=headers, params=params)
            if resp.status_code != 200:
                logger.warning(f"API HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            return resp.json()
        except Exception as e:
            logger.warning(f"fetch 失败: {e}")
            return None

    async def query_pet_size(self, diameter: float, weight: float) -> Optional[Dict[str, Any]]:
        """/api/v1/games/rocom/pet/size-query — 按身高(米)+体重(kg)反查。"""
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        params = {"diameter": diameter, "weight": weight}
        try:
            client = await self._get_client()
            resp = await client.get(
                f"{self.base_url}/api/v1/games/rocom/pet/size-query",
                headers=headers,
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(f"size-query HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            body = resp.json()
            # 兼容 {"code":0,"data":{...}} 外壳
            if isinstance(body, dict) and "data" in body and isinstance(body["data"], dict):
                return body["data"]
            return body
        except Exception as e:
            logger.warning(f"size-query 失败: {e}")
            return None


# ===== 订阅存储 =====

class SubscriptionStore:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if os.path.exists(self.file_path):
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        self._data = json.load(f) or {}
                except Exception as e:
                    logger.warning(f"读取订阅文件失败: {e}")
                    self._data = {}
            self._loaded = True

    async def _save(self) -> None:
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        tmp = self.file_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.file_path)

    async def upsert(self, key: str, sub: Dict[str, Any]) -> None:
        await self._ensure_loaded()
        async with self._lock:
            self._data[key] = sub
            await self._save()

    async def delete(self, key: str) -> bool:
        await self._ensure_loaded()
        async with self._lock:
            if key in self._data:
                del self._data[key]
                await self._save()
                return True
            return False

    async def all(self) -> Dict[str, Dict[str, Any]]:
        await self._ensure_loaded()
        async with self._lock:
            return json.loads(json.dumps(self._data))


# ===== 模块级懒单例 =====

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PLUGIN_DIR, "config.toml")
_client: Optional[MerchantClient] = None
_store: Optional[SubscriptionStore] = None
_renderer: Optional[MerchantRenderer] = None
_broadcast_task: Optional[asyncio.Task] = None
_config_getter: Optional[Callable[[str, Any], Any]] = None
_broadcast_runtime_enabled: Optional[bool] = None
_llm_rewrite_runtime_enabled: Optional[bool] = None
# 图片缓存：round_id -> PNG bytes；切轮次时自动清理旧项
_image_cache: Dict[str, bytes] = {}
_image_cache_lock = asyncio.Lock()


def set_config_getter(getter: Callable[[str, Any], Any]) -> None:
    """Command 每次 execute 时把自己的 get_config 绑定方法注册进来，供后台任务读配置。"""
    global _config_getter
    _config_getter = getter


def _cfg(key: str, default: Any = None) -> Any:
    if _config_getter is None:
        return default
    try:
        val = _config_getter(key, default)
    except Exception:
        return default
    return default if val is None else val


def get_client() -> MerchantClient:
    global _client
    base_url = str(_cfg("api.base_url", "https://wegame.shallow.ink"))
    api_key = str(_cfg("api.wegame_api_key", "") or "")
    timeout = int(_cfg("api.timeout", 15) or 15)
    if _client is None or _client.base_url != base_url.rstrip("/") or _client.api_key != api_key:
        _client = MerchantClient(base_url, api_key, timeout)
    return _client


def get_store() -> SubscriptionStore:
    global _store
    if _store is None:
        _store = SubscriptionStore(os.path.join(_PLUGIN_DIR, "data", "subscriptions.json"))
    return _store


# ===== 订阅 ↔ config.toml 双向同步 =====

def _items_str_to_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        # 逗号或空格分隔
        raw = value.replace("，", ",").replace("、", ",")
        tokens: List[str] = []
        for chunk in raw.split(","):
            tokens.extend([t for t in chunk.strip().split() if t])
        return tokens
    return []


async def sync_subscriptions_from_config() -> None:
    """把 config.toml 里 merchant.subscriptions 合并进 store。

    合并规则：
    - config 中新的 stream_id（store 里没有）→ 加入 store
    - 冲突时以 store 为准（避免 WebUI 改动覆盖命令最新状态）
    - 解析失败静默忽略
    """
    if tomllib is None or not os.path.exists(_CONFIG_PATH):
        return
    try:
        with open(_CONFIG_PATH, "rb") as f:
            conf = tomllib.load(f)
    except Exception as e:
        logger.warning(f"读 config.toml 失败: {e}")
        return
    configured = (conf.get("merchant") or {}).get("subscriptions") or []
    if not isinstance(configured, list) or not configured:
        return
    store = get_store()
    existing = await store.all()
    changed = False
    for item in configured:
        if not isinstance(item, dict):
            continue
        stream_id = str(item.get("stream_id") or "").strip()
        if not stream_id or stream_id in existing:
            continue
        sub = {
            "stream_id": stream_id,
            "group_id": str(item.get("group_id") or "").strip(),
            "items": _items_str_to_list(item.get("items")),
            "description": str(item.get("description") or "").strip(),
            "last_push_round": "",
        }
        await store.upsert(stream_id, sub)
        changed = True
    if changed:
        logger.info("[rocom_merchant] 已将 config.toml 中的新订阅合并到存储")


async def persist_subscriptions_to_config() -> None:
    """把 store 里的订阅全量回写 config.toml 的 merchant.subscriptions。"""
    if tomli_w is None or tomllib is None or not os.path.exists(_CONFIG_PATH):
        return
    store = get_store()
    all_subs = await store.all()
    try:
        with open(_CONFIG_PATH, "rb") as f:
            conf = tomllib.load(f)
    except Exception as e:
        logger.warning(f"读 config.toml 失败（跳过回写）: {e}")
        return
    merchant = conf.setdefault("merchant", {})
    merchant["subscriptions"] = [
        {
            "stream_id": sub.get("stream_id", k),
            "group_id": str(sub.get("group_id", "") or ""),
            "items": " ".join(sub.get("items", []) or []),
            "description": str(sub.get("description", "") or ""),
        }
        for k, sub in all_subs.items()
    ]
    try:
        tmp = _CONFIG_PATH + ".tmp"
        with open(tmp, "wb") as f:
            tomli_w.dump(conf, f)
        os.replace(tmp, _CONFIG_PATH)
    except Exception as e:
        logger.warning(f"写回 config.toml 失败: {e}")


def get_renderer() -> MerchantRenderer:
    global _renderer
    if _renderer is None:
        render_timeout = int(_cfg("merchant.render_timeout", 30000) or 30000)
        _renderer = MerchantRenderer(plugin_dir=_PLUGIN_DIR, render_timeout=render_timeout)
    return _renderer


def get_broadcast_runtime_enabled() -> bool:
    global _broadcast_runtime_enabled
    if _broadcast_runtime_enabled is None:
        _broadcast_runtime_enabled = bool(_cfg("merchant.broadcast_enabled", True))
    return _broadcast_runtime_enabled


def set_broadcast_runtime_enabled(value: bool) -> None:
    global _broadcast_runtime_enabled
    _broadcast_runtime_enabled = bool(value)


def get_llm_rewrite_runtime_enabled() -> bool:
    global _llm_rewrite_runtime_enabled
    if _llm_rewrite_runtime_enabled is None:
        _llm_rewrite_runtime_enabled = bool(_cfg("merchant.llm_rewrite_enabled", True))
    return _llm_rewrite_runtime_enabled


def set_llm_rewrite_runtime_enabled(value: bool) -> None:
    global _llm_rewrite_runtime_enabled
    _llm_rewrite_runtime_enabled = bool(value)


# ===== 时间 & 数据解析 =====

from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))


def current_round(now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(CN_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CN_TZ)
    start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    round_index: Optional[int] = None
    round_start: Optional[datetime] = None
    round_end: Optional[datetime] = None
    if start <= now < start + timedelta(hours=16):
        delta_seconds = int((now - start).total_seconds())
        round_index = delta_seconds // int(timedelta(hours=4).total_seconds()) + 1
        round_start = start + timedelta(hours=4 * (round_index - 1))
        round_end = round_start + timedelta(hours=4)
    return {
        "date": now.strftime("%Y-%m-%d"),
        "current": round_index,
        "total": 4,
        "round_id": f"{now.strftime('%Y-%m-%d')}-{round_index}"
        if round_index
        else f"{now.strftime('%Y-%m-%d')}-closed",
        "is_open": round_index is not None,
        "countdown": _format_countdown((round_end - now) if round_end else None),
        "start_time": round_start,
        "end_time": round_end,
    }


def _format_countdown(delta: Optional[timedelta]) -> str:
    if not delta:
        return "未开市"
    total = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0 and minutes > 0:
        return f"{hours}小时{minutes}分钟"
    if hours > 0:
        return f"{hours}小时"
    return f"{minutes}分钟"


def _format_window(item: Dict[str, Any]) -> str:
    start_ms = item.get("start_time")
    end_ms = item.get("end_time")
    if start_ms is None or end_ms is None:
        return "当前轮次"
    try:
        s = datetime.fromtimestamp(int(start_ms) / 1000, tz=CN_TZ).strftime("%m-%d %H:%M")
        e = datetime.fromtimestamp(int(end_ms) / 1000, tz=CN_TZ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "--"
    if s[:5] == e[:5]:
        return f"{s} - {e[6:]}"
    return f"{s} - {e}"


def parse_products(res: Optional[Dict[str, Any]]):
    payload = res or {}
    # 兼容 {"code":0,"message":"成功","data":{...}} 外壳
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]
    activities = payload.get("merchantActivities") or payload.get("merchant_activities") or []
    activity = activities[0] if activities else {}
    now_ms = int(datetime.now(CN_TZ).timestamp() * 1000)
    fallback_icon = "{{_res_path}}img/logo.cVSpb3sL.png"
    products: List[Dict[str, Any]] = []

    def is_active(item: Dict[str, Any]) -> bool:
        st, et = item.get("start_time"), item.get("end_time")
        if st is None or et is None:
            return True
        try:
            return int(st) <= now_ms < int(et)
        except (TypeError, ValueError):
            return True

    for key in ("get_props", "get_pets"):
        for item in activity.get(key) or []:
            if not is_active(item):
                continue
            products.append(
                {
                    "name": item.get("name", "未知"),
                    "image": item.get("icon_url") or fallback_icon,
                    "time_label": _format_window(item),
                }
            )
    return activity, products


async def render_merchant_image(
    activity: Dict[str, Any],
    products: List[Dict[str, Any]],
    round_info: Dict[str, Any],
) -> Optional[bytes]:
    data = {
        "background": "{{_res_path}}img/bg.C8CUoi7I.jpg",
        "titleIcon": True,
        "title": activity.get("name", "远行商人"),
        "subtitle": activity.get("start_date", "每日 08:00 / 12:00 / 16:00 / 20:00 刷新"),
        "product_count": len(products),
        "round_info": round_info,
        "products": products,
    }
    return await get_renderer().render_merchant(
        data=data,
        options={
            "device_scale_factor": 2,
            "viewport_width": 1200,
            "viewport_height": 900,
        },
    )


async def get_cached_merchant_image(
    activity: Dict[str, Any],
    products: List[Dict[str, Any]],
    round_info: Dict[str, Any],
    force_refresh: bool = False,
) -> Optional[bytes]:
    """按 round_id 缓存图片。同一轮只渲染一次；切轮次时清理旧项。

    为避免缓存里的倒计时显示过期，渲染时把 round_info.countdown 换成静态文字。
    实际倒计时由上层（命令/播报文本）单独拼接。
    """
    round_id = round_info.get("round_id") or "unknown"
    if not products:
        return None

    async with _image_cache_lock:
        if not force_refresh and round_id in _image_cache:
            return _image_cache[round_id]

    cached_round_info = dict(round_info)
    cached_round_info["countdown"] = "本轮进行中"
    img = await render_merchant_image(activity, products, cached_round_info)

    async with _image_cache_lock:
        # 清理非当前轮次的旧缓存
        for k in list(_image_cache.keys()):
            if k != round_id:
                _image_cache.pop(k, None)
        if img is not None:
            _image_cache[round_id] = img
    return img


async def prewarm_merchant_image() -> None:
    """后台预热当前轮次的图片缓存（无副作用的失败会被吞掉）。"""
    try:
        round_info = current_round()
        if not round_info["is_open"]:
            return
        client = get_client()
        res = await client.fetch(refresh=True)
        activity, products = parse_products(res)
        if not products:
            return
        await get_cached_merchant_image(activity, products, round_info)
    except Exception as e:
        logger.warning(f"预热图片缓存失败: {e}")


# ===== LLM 重写 =====

def _build_llm_model_config():
    llm_list: List[str] = _cfg("llm_config.llm_list", []) or []
    if llm_list and TaskConfig is not None:
        mc = TaskConfig()
        mc.model_list = llm_list
        mc.max_tokens = int(_cfg("llm_config.max_tokens", 512) or 512)
        mc.temperature = float(_cfg("llm_config.temperature", 0.9) or 0.9)
        mc.slow_threshold = float(_cfg("llm_config.slow_threshold", 30) or 30)
        mc.selection_strategy = _cfg("llm_config.selection_strategy", "balance")
        return mc
    group = _cfg("llm_config.llm_group", "utils")
    try:
        models = llm_api.get_available_models()
    except Exception as e:
        logger.warning(f"获取可用模型失败: {e}")
        return None
    mc = models.get(group) if models else None
    if not mc:
        logger.warning(f"未找到 LLM 分组 {group} 对应的模型配置")
    return mc


async def rewrite_broadcast(context: Dict[str, Any]) -> Optional[str]:
    prompt_template = _cfg("merchant.broadcast_prompt", DEFAULT_BROADCAST_PROMPT)
    try:
        prompt = prompt_template.format(**context)
    except (KeyError, IndexError) as e:
        logger.warning(f"播报 prompt 模板变量错误: {e}")
        return None
    model_config = _build_llm_model_config()
    if not model_config:
        return None
    try:
        success, response, _, _ = await llm_api.generate_with_model(
            prompt, model_config=model_config
        )
    except Exception as e:
        logger.warning(f"LLM 生成异常: {e}")
        return None
    if not success or not response:
        return None
    return str(response).strip() or None


# ===== 播报任务 =====

async def _broadcast_tick() -> None:
    store = get_store()
    all_subs = await store.all()
    if not all_subs:
        return
    round_info = current_round()
    if not round_info["is_open"]:
        return
    client = get_client()
    res = await client.fetch(refresh=True)
    _, products = parse_products(res)
    if not products:
        return
    product_names = {p["name"] for p in products}
    default_items = _cfg("merchant.default_items", []) or []

    for key, sub in all_subs.items():
        items = sub.get("items") or default_items
        matched = [n for n in items if n in product_names]
        if not matched:
            continue
        if sub.get("last_push_round") == round_info["round_id"]:
            continue

        stream_id = sub.get("stream_id") or ""
        if not stream_id:
            logger.warning(f"订阅缺少 stream_id，跳过: {key}")
            continue

        context = {
            "matched_items": "、".join(matched),
            "all_items": "、".join(sorted(product_names)),
            "round": round_info["current"] or "-",
            "total": round_info["total"],
            "countdown": round_info["countdown"],
            "date": round_info["date"],
        }
        text: Optional[str] = None
        if get_llm_rewrite_runtime_enabled():
            text = await rewrite_broadcast(context)
        if not text:
            text = (
                f"🛒 远行商人本轮命中订阅商品：{context['matched_items']}\n"
                f"当前轮次：第 {context['round']}/{context['total']} 轮  剩余：{context['countdown']}"
            )

        ok = await send_api.text_to_stream(
            text=text, stream_id=stream_id, storage_message=True
        )
        if not ok:
            logger.warning(f"播报推送失败: {key}")
            continue

        sub["last_push_round"] = round_info["round_id"]
        await store.upsert(key, sub)


async def _broadcast_loop() -> None:
    interval = int(_cfg("merchant.broadcast_interval", 300) or 300)
    logger.info(f"[rocom_merchant] 远行商人播报任务已启动，间隔 {interval}s")
    try:
        while True:
            try:
                # 每 tick 都预热缓存，让查询能命中（即使没订阅也预热）
                await prewarm_merchant_image()
                if get_broadcast_runtime_enabled():
                    await _broadcast_tick()
            except Exception as e:
                logger.error(f"播报 tick 异常: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("[rocom_merchant] 远行商人播报任务已取消")
        raise


def ensure_broadcast_task() -> None:
    """在有事件循环时幂等启动播报任务，并做一次 config→store 的订阅合并。"""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(sync_subscriptions_from_config())
    _broadcast_task = loop.create_task(_broadcast_loop())


# ===== 查蛋引擎单例 =====

from .egg_searcher import EggSearcher  # 延迟 import 避免循环依赖

_egg_searcher: Optional[EggSearcher] = None


def get_egg_searcher() -> EggSearcher:
    global _egg_searcher
    if _egg_searcher is None:
        _egg_searcher = EggSearcher(os.path.join(_PLUGIN_DIR, "data", "Pets.json"))
    return _egg_searcher
