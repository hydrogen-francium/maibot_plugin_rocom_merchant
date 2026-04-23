"""洛克王国图片渲染器（通用）。

- 模板用 Jinja2 语法。
- 模板内用 `{{_res_path}}` 引用插件目录下的资源（图片、CSS、字体），渲染时内联成 data URI。
- 用 Playwright 截图，返回 PNG bytes。
- 若模板带 `<link rel="stylesheet">` 指向本地 CSS，也会被内联。
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import uuid
from typing import Any, Dict, Optional

import jinja2

from src.plugin_system import get_logger

logger = get_logger("rocom_merchant.renderer")

# 首选内容容器，按先后顺序找，找到就以它做截图区
CONTAINER_SELECTORS = [
    ".merchant-page",
    ".searcheggs-cont",
    ".exchange-page",
    ".record-page",
    ".package-cont",
    ".bwiki-shell",
    ".skill-shell",
    ".page-section-main",
    ".lineup-page",
]


class MerchantRenderer:
    """Playwright 单实例通用渲染器。"""

    def __init__(self, plugin_dir: str, render_timeout: int = 30000):
        self.plugin_dir = plugin_dir
        # {{_res_path}} 对应的根 = 插件根目录
        self.res_root = plugin_dir
        self.template_root = os.path.join(plugin_dir, "render")
        self.render_timeout = render_timeout
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._env = jinja2.Environment(autoescape=True, keep_trailing_newline=True)

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None

    # ---- 模板 & 资源内联 ----

    def _load_template(self, template_name: str) -> str:
        path = os.path.join(self.template_root, template_name)
        if not os.path.exists(path):
            logger.error(f"模板不存在: {path}")
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _read_bytes(self, rel_path: str) -> Optional[bytes]:
        path = os.path.join(self.res_root, rel_path)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def _inline_local_data_uri(self, rel_path: str) -> Optional[str]:
        raw = self._read_bytes(rel_path)
        if raw is None:
            return None
        mime = mimetypes.guess_type(os.path.join(self.res_root, rel_path))[0] or "application/octet-stream"
        b64 = base64.b64encode(raw).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    def _inline_css_content(self, rel_path: str) -> Optional[str]:
        raw = self._read_bytes(rel_path)
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        # 再把 CSS 里 {{_res_path}}xxx 的资源也内联（字体、背景图等）
        return self._inline_assets(text, in_css=True)

    def _inline_assets(self, source: str, in_css: bool = False) -> str:
        """内联 HTML 或 CSS 里的本地资源与 CSS link。"""

        # 1. <link rel="stylesheet" href="{{_res_path}}xxx.css"> → 内联 CSS
        if not in_css:
            def replace_link(match: re.Match) -> str:
                rel = match.group(1)
                css = self._inline_css_content(rel)
                return f"<style>\n{css}\n</style>" if css is not None else match.group(0)

            source = re.sub(
                r'<link[^>]+rel="stylesheet"[^>]+href="\{\{_res_path\}\}([^"]+\.css)"[^/>]*/?>',
                replace_link,
                source,
            )

        # 2. src="{{_res_path}}xxx.png" → src="data:..."
        def replace_src(match: re.Match) -> str:
            rel = match.group(1)
            uri = self._inline_local_data_uri(rel)
            return f'src="{uri}"' if uri else match.group(0)

        source = re.sub(
            r'src="\{\{_res_path\}\}([^"]+\.(?:png|jpg|jpeg|gif|svg|webp))"',
            replace_src,
            source,
        )

        # 3. url({{_res_path}}xxx) → url(data:...)
        def replace_url(match: re.Match) -> str:
            rel = match.group(1)
            uri = self._inline_local_data_uri(rel)
            return f"url({uri})" if uri else match.group(0)

        source = re.sub(
            r"url\(\s*['\"]?\{\{_res_path\}\}([^)\"']+?)['\"]?\s*\)",
            replace_url,
            source,
        )
        return source

    # ---- 对外入口 ----

    async def render_html(
        self,
        template_name: str,
        data: Dict[str, Any],
        options: Optional[Dict] = None,
    ) -> Optional[bytes]:
        """渲染指定模板，返回 PNG bytes。"""
        template = self._load_template(template_name)
        if not template:
            return None
        inlined = self._inline_assets(template)
        try:
            render_data = dict(data)
            render_data.setdefault("_res_path", "")
            html = self._env.from_string(inlined).render(**render_data)
        except Exception as e:
            logger.error(f"Jinja2 渲染错误: {e}")
            return None
        return await self._screenshot(html, template_name, options or {})

    async def render_merchant(
        self,
        data: Dict[str, Any],
        options: Optional[Dict] = None,
    ) -> Optional[bytes]:
        """渲染远行商人模板。"""
        return await self.render_html("yuanxing-shangren/index.html", data, options)

    # ---- Playwright 截图 ----

    async def _ensure_browser(self) -> None:
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await self._playwright.chromium.launch()

    async def _screenshot(
        self, html: str, template_name: str, options: Dict[str, Any]
    ) -> Optional[bytes]:
        try:
            async with self._lock:
                await self._ensure_browser()

            device_scale_factor = float(options.get("device_scale_factor", 2.0))
            viewport_width = int(options.get("viewport_width", 1600))
            viewport_height = int(options.get("viewport_height", 1200))

            context = await self._browser.new_context(
                device_scale_factor=device_scale_factor,
                viewport={"width": viewport_width, "height": viewport_height},
            )
            page = await context.new_page()

            template_dir = os.path.dirname(os.path.join(self.template_root, template_name))
            os.makedirs(template_dir, exist_ok=True)
            temp_html = os.path.join(template_dir, f"_tmp_{uuid.uuid4().hex[:8]}.html")
            try:
                with open(temp_html, "w", encoding="utf-8") as f:
                    f.write(html)
                try:
                    await page.goto(
                        f"file:///{temp_html.replace(chr(92), '/')}",
                        wait_until="networkidle",
                        timeout=self.render_timeout,
                    )
                except Exception:
                    pass

                await page.evaluate(
                    """
                    Promise.all(Array.from(document.images).map(img => {
                        if (img.complete) return Promise.resolve();
                        return new Promise(resolve => {
                            img.onload = resolve;
                            img.onerror = resolve;
                        });
                    }))
                    """
                )
                await page.wait_for_timeout(300)

                # 按优先级找截图容器
                handle = None
                for selector in CONTAINER_SELECTORS:
                    handle = await page.query_selector(selector)
                    if handle:
                        break
                if handle:
                    box = await handle.bounding_box()
                    if box:
                        await page.set_viewport_size(
                            {
                                "width": max(int(box["width"]) + 8, 200),
                                "height": max(int(box["height"]) + 8, 200),
                            }
                        )
                        await page.wait_for_timeout(100)
                    png_bytes = await handle.screenshot(type="png")
                else:
                    png_bytes = await page.screenshot(full_page=True, type="png")

                return png_bytes
            finally:
                try:
                    if os.path.exists(temp_html):
                        os.remove(temp_html)
                except Exception:
                    pass
                await page.close()
                await context.close()
        except Exception as e:
            logger.error(f"Playwright 截图失败: {e}")
            return None
