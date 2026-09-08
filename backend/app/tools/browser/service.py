"""Playwright browser service.

One persistent Chromium context (cookies survive restarts, so logins such as
WhatsApp Web persist) shared by all browser tools.  Lazily started on first use.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SEARCH_URLS = {
    "duckduckgo": "https://duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
}


class BrowserService:
    def __init__(self, profile_dir: str, headless: bool = False, download_dir: str = ""):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.download_dir = Path(download_dir) if download_dir else None
        self._pw = None
        self._context = None
        self._lock = asyncio.Lock()
        self.available: Optional[bool] = None
        self.error: str = ""

    async def start(self):
        async with self._lock:
            if self._context is not None:
                return self._context
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                self.available = False
                self.error = "playwright is not installed (pip install playwright && playwright install chromium)"
                raise RuntimeError(self.error) from e
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._pw = await async_playwright().start()
                kwargs: Dict[str, Any] = dict(headless=self.headless, viewport={"width": 1280, "height": 900}, accept_downloads=True,
                                              args=["--disable-blink-features=AutomationControlled"])
                try:
                    self._context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), channel="chrome", **kwargs)
                except Exception:  # noqa: BLE001  - no system Chrome; use bundled chromium
                    self._context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
            except Exception as e:  # noqa: BLE001
                self.available = False
                self.error = f"Could not start the browser: {e}. Run `playwright install chromium`."
                log.error(self.error)
                raise RuntimeError(self.error) from e
            self.available = True
            return self._context

    async def page(self, new: bool = False):
        ctx = await self.start()
        pages = [p for p in ctx.pages if not p.is_closed()]
        if pages and not new:
            return pages[-1]
        return await ctx.new_page()

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._pw = None

    def health(self) -> Dict[str, Any]:
        try:
            import playwright  # noqa: F401

            installed = True
        except ImportError:
            installed = False
        return {"ok": installed and self.available is not False, "installed": installed, "running": self._context is not None,
                "detail": self.error or ("running" if self._context else "idle")}

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    async def extract_text(page, max_chars: int = 15000) -> str:
        text = await page.evaluate(
            """() => {
                const clone = document.body ? document.body.cloneNode(true) : null;
                if (!clone) return '';
                clone.querySelectorAll('script,style,noscript,svg,iframe,nav,footer,header,[aria-hidden="true"]').forEach(e => e.remove());
                return clone.innerText || '';
            }"""
        )
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        return text[:max_chars]

    @staticmethod
    async def interactive_elements(page, limit: int = 60) -> List[Dict[str, Any]]:
        return await page.evaluate(
            """(limit) => {
                const out = [];
                const els = document.querySelectorAll('a[href],button,input,textarea,select,[role=button],[onclick]');
                for (const el of els) {
                    if (out.length >= limit) break;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || '').trim().slice(0, 80);
                    if (!label && el.tagName !== 'INPUT') continue;
                    out.push({index: out.length, tag: el.tagName.toLowerCase(), type: el.type || null, text: label,
                              name: el.name || null, id: el.id || null, href: el.href || null});
                }
                return out;
            }""",
            limit,
        )

    @staticmethod
    async def search_results(page, engine: str) -> List[Dict[str, str]]:
        if engine == "duckduckgo":
            return await page.evaluate(
                """() => Array.from(document.querySelectorAll('.result')).slice(0, 10).map(r => ({
                    title: (r.querySelector('.result__a') || {}).innerText || '',
                    url: (r.querySelector('.result__a') || {}).href || '',
                    snippet: (r.querySelector('.result__snippet') || {}).innerText || ''}))"""
            )
        if engine == "bing":
            return await page.evaluate(
                """() => Array.from(document.querySelectorAll('li.b_algo')).slice(0, 10).map(r => ({
                    title: (r.querySelector('h2 a') || {}).innerText || '',
                    url: (r.querySelector('h2 a') || {}).href || '',
                    snippet: (r.querySelector('.b_caption p, p') || {}).innerText || ''}))"""
            )
        return await page.evaluate(
            """() => Array.from(document.querySelectorAll('div.g, div[data-sokoban-container]')).slice(0, 10).map(r => ({
                title: (r.querySelector('h3') || {}).innerText || '',
                url: (r.querySelector('a') || {}).href || '',
                snippet: (r.querySelector('div[data-sncf], .VwiC3b, span') || {}).innerText || ''})).filter(x => x.title)"""
        )
