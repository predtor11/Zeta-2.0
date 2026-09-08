"""Browser tools built on Playwright: open, search, read, click, fill, download, upload, screenshot.

Everything the browser returns is UNTRUSTED content.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import httpx
from pydantic import BaseModel, Field

from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path, validate_url
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.browser.service import SEARCH_URLS, BrowserService


def _svc(ctx: ToolContext) -> BrowserService:
    return ctx.service("browser")


async def _page(ctx: ToolContext, new: bool = False):
    try:
        return await _svc(ctx).page(new=new)
    except RuntimeError as e:
        raise RuntimeError(str(e))


class OpenUrlArgs(BaseModel):
    url: str = Field(description="Website URL")
    new_tab: bool = False
    wait_seconds: float = Field(default=1.0, ge=0, le=15)


class OpenWebsiteTool(Tool):
    name = "open_website"
    description = "Open a URL in Zeta's browser and return the page title and a text summary of the content."
    category = "browser"
    risk_level = RiskLevel.SAFE
    args_model = OpenUrlArgs
    timeout_seconds = 90

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Open {args.get('url')}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = validate_url(args["url"])
        ctx.activity(f"Opening {url}")
        try:
            page = await _page(ctx, new=args.get("new_tab", False))
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(args.get("wait_seconds", 1.0))
            title = await page.title()
            text = await BrowserService.extract_text(page, 6000)
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Could not open {url}: {e.__class__.__name__}: {str(e)[:200]}")
        return ToolResult.ok({"url": page.url, "title": title, "text": text}, summary=f"Opened {title or url}", untrusted=True, source="web")


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    engine: str = Field(default="", description="duckduckgo | bing | google (default from config)")
    fetch_top: int = Field(default=0, ge=0, le=3, description="Also fetch the text of the top N results")


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return the top results (title, url, snippet). Optionally fetch the text of the top results."
    category = "browser"
    risk_level = RiskLevel.SAFE
    args_model = SearchArgs
    timeout_seconds = 120

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Search the web for '{args.get('query')}'"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        engine = (args.get("engine") or ctx.settings.browser_search_engine).lower()
        if engine not in SEARCH_URLS:
            engine = "duckduckgo"
        q = args["query"]
        ctx.activity(f"Searching {engine} for '{q}'")
        results: List[Dict[str, str]] = []
        # Fast path: DuckDuckGo HTML endpoint via plain HTTP (no browser needed)
        if engine == "duckduckgo":
            results = await _ddg_http(q)
        if not results:
            try:
                page = await _page(ctx)
                await page.goto(SEARCH_URLS[engine].format(q=quote_plus(q)), wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.0)
                results = await BrowserService.search_results(page, engine)
            except RuntimeError as e:
                return ToolResult.fail(str(e))
            except Exception as e:  # noqa: BLE001
                return ToolResult.fail(f"Search failed: {e.__class__.__name__}: {str(e)[:200]}")
        results = [r for r in results if r.get("url")][:10]
        fetched = []
        for r in results[: args.get("fetch_top", 0)]:
            fetched.append({"url": r["url"], "text": await _fetch_text(ctx, r["url"], 4000)})
        return ToolResult.ok({"engine": engine, "query": q, "results": results, "pages": fetched},
                             summary=f"{len(results)} results for '{q}'", untrusted=True, source="web")


async def _ddg_http(q: str) -> List[Dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Zeta/1.0"}) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": q})
        if r.status_code != 200:
            return []
        html = r.text
        out = []
        for m in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', html, re.S):
            url, title, snippet = m.group(1), _strip(m.group(2)), _strip(m.group(3))
            if url.startswith("//duckduckgo.com/l/?uddg="):
                from urllib.parse import parse_qs, unquote, urlparse

                url = unquote(parse_qs(urlparse("https:" + url).query).get("uddg", [url])[0])
            out.append({"title": title, "url": url, "snippet": snippet})
            if len(out) >= 10:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


async def _fetch_text(ctx: ToolContext, url: str, max_chars: int) -> str:
    try:
        page = await _page(ctx)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(0.8)
        return await BrowserService.extract_text(page, max_chars)
    except Exception as e:  # noqa: BLE001
        return f"(could not fetch: {e.__class__.__name__})"


class ReadPageArgs(BaseModel):
    max_chars: int = Field(default=12000, ge=500, le=60000)
    include_elements: bool = Field(default=True, description="Also list clickable elements/inputs with indexes")


class ReadPageTool(Tool):
    name = "read_page"
    description = "Read the text of the current browser page and list its interactive elements (links, buttons, inputs) with indexes."
    category = "browser"
    risk_level = RiskLevel.READ_ONLY
    args_model = ReadPageArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            page = await _page(ctx)
            text = await BrowserService.extract_text(page, args.get("max_chars", 12000))
            elements = await BrowserService.interactive_elements(page) if args.get("include_elements", True) else []
            return ToolResult.ok({"url": page.url, "title": await page.title(), "text": text, "elements": elements},
                                 summary="Page read", untrusted=True, source="web")
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Could not read page: {e.__class__.__name__}: {str(e)[:200]}")


class ClickArgs(BaseModel):
    selector: str = Field(default="", description="CSS selector, or text to click (e.g. 'Sign in')")
    element_index: int = Field(default=-1, description="Index from read_page's elements list")
    wait_seconds: float = Field(default=1.0, ge=0, le=15)


class ClickElementTool(Tool):
    name = "click_element"
    description = "Click an element on the current page by CSS selector, visible text, or element index from read_page."
    category = "browser"
    risk_level = RiskLevel.SAFE
    args_model = ClickArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        t = (args.get("selector") or "").lower()
        if any(w in t for w in ("buy", "pay", "purchase", "order", "checkout", "delete", "send", "submit", "confirm", "transfer")):
            return RiskLevel.SENSITIVE
        return RiskLevel.SAFE

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Click '{args.get('selector') or ('element #' + str(args.get('element_index')))}'"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            page = await _page(ctx)
            if args.get("element_index", -1) >= 0:
                elements = await BrowserService.interactive_elements(page)
                idx = args["element_index"]
                if idx >= len(elements):
                    return ToolResult.fail(f"No element with index {idx}")
                el = elements[idx]
                loc = page.locator(f"#{el['id']}") if el.get("id") else page.get_by_text(el["text"], exact=False).first if el.get("text") else None
                if loc is None:
                    return ToolResult.fail("Element cannot be located reliably; use a selector.")
                await loc.click(timeout=10000)
            else:
                sel = args["selector"]
                try:
                    await page.click(sel, timeout=5000)
                except Exception:  # noqa: BLE001
                    await page.get_by_text(sel, exact=False).first.click(timeout=10000)
            await asyncio.sleep(args.get("wait_seconds", 1.0))
            return ToolResult.ok({"url": page.url, "title": await page.title()}, summary="Clicked", untrusted=True, source="web")
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Click failed: {e.__class__.__name__}: {str(e)[:200]}")


class FillArgs(BaseModel):
    fields: Dict[str, str] = Field(description="Map of CSS selector / placeholder / label -> value to type")
    submit: bool = Field(default=False, description="Press Enter in the last field afterwards")


class FillFormTool(Tool):
    name = "fill_form"
    description = "Fill input fields on the current page (by CSS selector, placeholder or label text), optionally submitting."
    category = "browser"
    risk_level = RiskLevel.SENSITIVE
    args_model = FillArgs
    log_arguments = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Fill {len(args.get('fields', {}))} field(s)" + (" and submit" if args.get("submit") else "")

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            page = await _page(ctx)
            last = None
            for key, value in args["fields"].items():
                loc = None
                for candidate in (page.locator(key), page.get_by_placeholder(key), page.get_by_label(key)):
                    try:
                        if await candidate.count() > 0:
                            loc = candidate.first
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if loc is None:
                    return ToolResult.fail(f"Field '{key}' not found on the page")
                await loc.fill(value, timeout=10000)
                last = loc
            if args.get("submit") and last is not None:
                await last.press("Enter")
                await asyncio.sleep(1.5)
            return ToolResult.ok({"url": page.url, "filled": list(args["fields"].keys())}, summary="Form filled", untrusted=True, source="web")
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Fill failed: {e.__class__.__name__}: {str(e)[:200]}")


class DownloadArgs(BaseModel):
    url: str = Field(default="", description="Direct file URL to download (preferred)")
    click_selector: str = Field(default="", description="Or: selector/text of a download link/button on the current page")
    save_as: str = Field(default="", description="Destination file path (default: Downloads folder)")


class DownloadFileTool(Tool):
    name = "download_file"
    description = "Download a file from a URL (or by clicking a download link) into the Downloads folder or a given path."
    category = "browser"
    risk_level = RiskLevel.SENSITIVE
    args_model = DownloadArgs
    timeout_seconds = 600

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Download {args.get('url') or args.get('click_selector')} -> {args.get('save_as') or 'Downloads'}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        roots = ctx.settings.allowed_roots
        dest_dir = Path(ctx.settings.browser_download_dir)
        dest: Optional[Path] = resolve_path(args["save_as"], roots, for_write=True) if args.get("save_as") else None
        if args.get("url"):
            url = validate_url(args["url"])
            ctx.activity(f"Downloading {url}")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=300) as c:
                    async with c.stream("GET", url) as r:
                        if r.status_code >= 400:
                            return ToolResult.fail(f"Server returned HTTP {r.status_code}")
                        if dest is None:
                            name = None
                            cd = r.headers.get("content-disposition", "")
                            m = re.search(r'filename\*?="?([^";]+)', cd)
                            if m:
                                name = m.group(1).split("''")[-1]
                            name = name or url.split("?")[0].rstrip("/").split("/")[-1] or "download.bin"
                            dest = resolve_path(str(dest_dir / name), roots, for_write=True)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        size = 0
                        with open(dest, "wb") as f:
                            async for chunk in r.aiter_bytes():
                                f.write(chunk)
                                size += len(chunk)
            except httpx.HTTPError as e:
                return ToolResult.fail(f"Download failed: {e.__class__.__name__}")
            return ToolResult.ok({"path": str(dest), "bytes": size}, summary=f"Downloaded {dest.name}", artifacts={"path": str(dest)})
        if args.get("click_selector"):
            try:
                page = await _page(ctx)
                async with page.expect_download(timeout=120000) as dl_info:
                    try:
                        await page.click(args["click_selector"], timeout=5000)
                    except Exception:  # noqa: BLE001
                        await page.get_by_text(args["click_selector"], exact=False).first.click(timeout=10000)
                dl = await dl_info.value
                if dest is None:
                    dest = resolve_path(str(dest_dir / dl.suggested_filename), roots, for_write=True)
                await dl.save_as(str(dest))
                return ToolResult.ok({"path": str(dest)}, summary=f"Downloaded {dest.name}", artifacts={"path": str(dest)})
            except RuntimeError as e:
                return ToolResult.fail(str(e))
            except Exception as e:  # noqa: BLE001
                return ToolResult.fail(f"Download failed: {e.__class__.__name__}: {str(e)[:200]}")
        return ToolResult.fail("Provide url or click_selector")


class UploadArgs(BaseModel):
    selector: str = Field(description="CSS selector of the file input (e.g. input[type=file])")
    path: str = Field(description="Local file to upload")


class UploadFileTool(Tool):
    name = "upload_file"
    description = "Upload a local file into a file input on the current page."
    category = "browser"
    risk_level = RiskLevel.SENSITIVE
    args_model = UploadArgs

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Upload {args.get('path')} to the current page"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, must_exist=True)
        try:
            page = await _page(ctx)
            await page.set_input_files(args["selector"], str(p), timeout=10000)
            return ToolResult.ok({"uploaded": str(p), "url": page.url}, summary=f"Uploaded {p.name}")
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Upload failed: {e.__class__.__name__}: {str(e)[:200]}")


class PageScreenshotArgs(BaseModel):
    full_page: bool = False


class PageScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Screenshot the current browser page (saved under data/screenshots)."
    category = "browser"
    risk_level = RiskLevel.READ_ONLY
    args_model = PageScreenshotArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        import base64
        from datetime import datetime

        d = ctx.settings.data_dir / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        try:
            page = await _page(ctx)
            data = await page.screenshot(path=str(out), full_page=args.get("full_page", False))
        except RuntimeError as e:
            return ToolResult.fail(str(e))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Screenshot failed: {e.__class__.__name__}")
        return ToolResult.ok({"path": str(out), "url": page.url}, summary="Page screenshot saved",
                             artifacts={"image_b64": base64.b64encode(data).decode(), "path": str(out)})


class HttpGetArgs(BaseModel):
    url: str
    max_chars: int = Field(default=15000, ge=500, le=100000)
    json_response: bool = Field(default=False, description="Parse JSON (for APIs)")


class HttpGetTool(Tool):
    name = "http_get"
    description = "Fetch a URL or public API directly (no browser). Prefer this over browsing for APIs and simple pages."
    category = "browser"
    risk_level = RiskLevel.SAFE
    args_model = HttpGetArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = validate_url(args["url"])
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0 Zeta/1.0"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            return ToolResult.fail(f"Request failed: {e.__class__.__name__}")
        ctype = r.headers.get("content-type", "")
        if args.get("json_response") or "application/json" in ctype:
            try:
                body: Any = r.json()
            except ValueError:
                body = r.text[: args.get("max_chars", 15000)]
        elif "text/html" in ctype:
            body = _strip(re.sub(r"<(script|style)[^>]*>.*?</\1>", "", r.text, flags=re.S))[: args.get("max_chars", 15000)]
        else:
            body = r.text[: args.get("max_chars", 15000)]
        return ToolResult(success=r.status_code < 400, output={"status": r.status_code, "content_type": ctype, "body": body},
                          error=None if r.status_code < 400 else f"HTTP {r.status_code}", summary=f"GET {url} -> {r.status_code}",
                          untrusted=True, source="web")


def _build(settings) -> List[Tool]:
    return [OpenWebsiteTool(), WebSearchTool(), ReadPageTool(), ClickElementTool(), FillFormTool(), DownloadFileTool(),
            UploadFileTool(), PageScreenshotTool(), HttpGetTool()]


PLUGIN = Plugin(
    name="browser",
    description="Web browsing, search, extraction and form automation via Playwright",
    permissions=["browser"],
    tools=_build,
    configuration={"BROWSER_HEADLESS": "true|false", "BROWSER_SEARCH_ENGINE": "duckduckgo|bing|google", "BROWSER_DOWNLOAD_DIR": "download folder"},
)
