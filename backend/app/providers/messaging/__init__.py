"""Messaging providers (WhatsApp).

    MessagingProvider
    ├── WhatsAppWebProvider       Playwright automation of web.whatsapp.com (scan QR once)
    ├── WhatsAppBusinessProvider  Meta Cloud API (official)
    └── WAPIProvider              Generic HTTP gateway (WAPI-style: base URL + token + instance)

Contacts are resolved from a local JSON file (CONTACTS_FILE) so the LLM never
has to guess phone numbers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from app.core.config import Settings, WhatsAppProviderName
from app.core.exceptions import ConfigurationError, ProviderError

log = logging.getLogger(__name__)


@dataclass
class Contact:
    name: str
    phone: str = ""
    email: str = ""
    aliases: List[str] = None  # type: ignore

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "phone": self.phone, "email": self.email, "aliases": self.aliases or []}


class ContactBook:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> List[Contact]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("contacts file invalid: %s", e)
            return []
        return [Contact(name=c.get("name", ""), phone=str(c.get("phone", "")), email=c.get("email", ""), aliases=c.get("aliases", []))
                for c in data if c.get("name")]

    def save(self, contacts: List[Contact]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([c.to_dict() for c in contacts], indent=2, ensure_ascii=False), encoding="utf-8")

    def resolve(self, query: str) -> List[Contact]:
        q = query.strip().lower()
        if not q:
            return []
        digits = re.sub(r"\D", "", q)
        contacts = self.load()
        if digits and len(digits) >= 7:
            return [c for c in contacts if re.sub(r"\D", "", c.phone).endswith(digits)] or [Contact(name=query, phone=digits)]
        exact = [c for c in contacts if c.name.lower() == q or q in [a.lower() for a in (c.aliases or [])]]
        if exact:
            return exact
        return [c for c in contacts if q in c.name.lower() or any(q in a.lower() for a in (c.aliases or []))]

    def add(self, name: str, phone: str = "", email: str = "") -> Contact:
        contacts = self.load()
        for c in contacts:
            if c.name.lower() == name.lower():
                c.phone = phone or c.phone
                c.email = email or c.email
                self.save(contacts)
                return c
        c = Contact(name=name, phone=phone, email=email, aliases=[])
        contacts.append(c)
        self.save(contacts)
        return c


class MessagingProvider(ABC):
    name = "base"
    supports_name_lookup = False  # True when the provider can find a chat by contact name itself (WhatsApp Web)

    @abstractmethod
    async def send_message(self, phone: str, text: str) -> Dict[str, Any]: ...

    async def send_to_name(self, name: str, text: str) -> Dict[str, Any]:
        raise ConfigurationError(f"{self.name} cannot look up chats by name; add the contact's number with add_contact.")

    async def read_recent(self, chat: str, limit: int = 20) -> List[Dict[str, Any]]:
        raise ConfigurationError(f"{self.name} does not support reading messages (NOT IMPLEMENTED)")

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "detail": self.name}


class DisabledMessaging(MessagingProvider):
    name = "disabled"

    async def send_message(self, phone: str, text: str) -> Dict[str, Any]:
        raise ConfigurationError("WhatsApp is not configured. Set WHATSAPP_PROVIDER=web (WhatsApp Web), business, or wapi.")

    async def health(self) -> Dict[str, Any]:
        return {"ok": False, "detail": "disabled"}


class WhatsAppWebProvider(MessagingProvider):
    """Drives web.whatsapp.com in Zeta's persistent browser profile.

    First use: the browser opens WhatsApp Web; scan the QR code with your phone.
    The session persists in the browser profile directory afterwards.
    """

    name = "whatsapp_web"
    supports_name_lookup = True

    def __init__(self, browser_service):
        self.browser = browser_service

    async def _page(self):
        page = await self.browser.page(new=False)
        if "web.whatsapp.com" not in page.url:
            page = await self.browser.page(new=True)
            await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
        return page

    async def is_logged_in(self, page, timeout_s: float = 20) -> bool:
        try:
            await page.wait_for_selector("div[data-testid='chat-list'], #pane-side, canvas[aria-label*='Scan']", timeout=int(timeout_s * 1000))
        except Exception:  # noqa: BLE001
            return False
        return await page.locator("#pane-side, div[data-testid='chat-list']").count() > 0

    async def send_message(self, phone: str, text: str) -> Dict[str, Any]:
        page = await self._page()
        if not await self.is_logged_in(page):
            raise ProviderError("not logged in", user_message="WhatsApp Web is not logged in. Scan the QR code in the Zeta browser window, then try again.")
        await page.goto(f"https://web.whatsapp.com/send?phone={phone}&text={quote(text)}", wait_until="domcontentloaded", timeout=60000)
        try:
            box = page.locator("footer div[contenteditable='true'], div[data-testid='conversation-compose-box-input']").first
            await box.wait_for(timeout=45000)
        except Exception:  # noqa: BLE001
            invalid = await page.locator("text=/phone number shared via url is invalid/i").count()
            if invalid:
                raise ProviderError("invalid number", user_message=f"WhatsApp says +{phone} is not a valid WhatsApp number.")
            raise ProviderError("compose box not found", user_message="WhatsApp Web did not open the chat. Is the number registered on WhatsApp?")
        await asyncio.sleep(1.0)
        # The text was prefilled via URL; verify then press Enter
        content = (await box.inner_text()).strip()
        if not content:
            await box.fill(text)
        await box.press("Enter")
        await asyncio.sleep(1.5)
        return {"sent": True, "phone": phone, "provider": self.name}

    async def _open_chat_by_name(self, page, name: str) -> str:
        """Type the name into WhatsApp Web's search box and open the best matching chat. Returns the chat title."""
        if "web.whatsapp.com" not in page.url or "/send" in page.url:
            await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("#pane-side, div[data-testid='chat-list']", timeout=45000)
        search = page.locator("div[contenteditable='true'][data-tab='3'], div[title='Search input textbox'], "
                              "div[aria-label='Search input textbox'], div[role='textbox'][contenteditable='true']").first
        await search.click(timeout=15000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await search.type(name, delay=40)
        await asyncio.sleep(2.0)
        pane = page.locator("#pane-side, div[data-testid='chat-list']").first
        rows = pane.locator("div[role='listitem'], div[role='row'], div[data-testid='cell-frame-container']")
        n = await rows.count()
        target = None
        title = ""
        wanted = name.strip().lower()
        for i in range(min(n, 12)):
            row = rows.nth(i)
            try:
                t = (await row.locator("span[title]").first.get_attribute("title", timeout=2000)) or ""
            except Exception:  # noqa: BLE001
                continue
            tl = t.strip().lower()
            if tl == wanted or wanted in tl or all(w in tl for w in wanted.split()):
                target, title = row, t.strip()
                break
        if target is None:
            # Fall back to the first result only if it is a real chat row with a title
            for i in range(min(n, 12)):
                row = rows.nth(i)
                try:
                    t = (await row.locator("span[title]").first.get_attribute("title", timeout=1500)) or ""
                except Exception:  # noqa: BLE001
                    continue
                if t.strip():
                    target, title = row, t.strip()
                    break
        if target is None:
            await page.keyboard.press("Escape")
            raise ProviderError("no chat", user_message=f"I couldn't find a WhatsApp chat or contact named '{name}'. Please give me the phone number.")
        await target.click()
        box = page.locator("footer div[contenteditable='true'], div[data-testid='conversation-compose-box-input']").first
        await box.wait_for(timeout=30000)
        return title

    async def send_to_name(self, name: str, text: str) -> Dict[str, Any]:
        page = await self._page()
        if not await self.is_logged_in(page):
            raise ProviderError("not logged in", user_message="WhatsApp Web is not logged in. Scan the QR code in the Zeta browser window, then try again.")
        title = await self._open_chat_by_name(page, name)
        box = page.locator("footer div[contenteditable='true'], div[data-testid='conversation-compose-box-input']").first
        await box.click()
        for i, line in enumerate(text.split("\n")):
            if i:
                await page.keyboard.press("Shift+Enter")
            await box.type(line, delay=10)
        await asyncio.sleep(0.5)
        await box.press("Enter")
        await asyncio.sleep(1.5)
        return {"sent": True, "chat": title, "provider": self.name}

    async def read_recent(self, chat: str, limit: int = 20) -> List[Dict[str, Any]]:
        page = await self._page()
        if not await self.is_logged_in(page):
            raise ProviderError("not logged in", user_message="WhatsApp Web is not logged in.")
        search = page.locator("div[contenteditable='true'][data-tab='3'], div[title='Search input textbox']").first
        await search.click(timeout=15000)
        await search.fill(chat)
        await asyncio.sleep(1.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        msgs = await page.evaluate(
            """(limit) => Array.from(document.querySelectorAll('div.message-in, div.message-out')).slice(-limit).map(m => ({
                direction: m.classList.contains('message-out') ? 'out' : 'in',
                text: (m.querySelector('span.selectable-text') || {}).innerText || '',
                time: (m.querySelector('[data-pre-plain-text]') || {}).getAttribute ? (m.querySelector('[data-pre-plain-text]') || {}).getAttribute('data-pre-plain-text') : ''}))""",
            limit,
        )
        return msgs

    async def health(self) -> Dict[str, Any]:
        h = self.browser.health()
        return {"ok": h["ok"], "detail": f"WhatsApp Web via browser ({h['detail']})"}


class WhatsAppBusinessProvider(MessagingProvider):
    name = "whatsapp_business"

    def __init__(self, token: str, phone_number_id: str, api_version: str = "v20.0"):
        self._token = token
        self.phone_number_id = phone_number_id
        self.api_version = api_version

    async def send_message(self, phone: str, text: str) -> Dict[str, Any]:
        if not self._token or not self.phone_number_id:
            raise ConfigurationError("WHATSAPP_BUSINESS_TOKEN and WHATSAPP_BUSINESS_PHONE_ID are required.")
        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, headers={"Authorization": f"Bearer {self._token}"}, json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="The WhatsApp Business API could not be reached.") from e
        if r.status_code >= 400:
            raise ProviderError(r.text[:300], user_message=f"WhatsApp Business API error ({r.status_code}).")
        data = r.json()
        return {"sent": True, "phone": phone, "provider": self.name, "message_id": (data.get("messages") or [{}])[0].get("id")}

    async def health(self) -> Dict[str, Any]:
        ok = bool(self._token and self.phone_number_id)
        return {"ok": ok, "detail": "configured" if ok else "missing token/phone id"}


class WAPIProvider(MessagingProvider):
    """Generic HTTP gateway.  Configure WAPI_BASE_URL, WAPI_TOKEN, WAPI_INSTANCE_ID.

    Request shape (common to WAPI-style gateways such as UltraMsg / green-api / wapi.js):
        POST {base_url}/instance{instance}/messages/chat?token={token}   body: {to: <phone>, body: <text>}
    Adjust `send_path`/`payload` in a plugin subclass for a different gateway.
    """

    name = "wapi"

    def __init__(self, base_url: str, token: str, instance_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.instance_id = instance_id

    def send_path(self) -> str:
        return f"/instance{self.instance_id}/messages/chat" if self.instance_id else "/messages/chat"

    def payload(self, phone: str, text: str) -> Dict[str, Any]:
        return {"to": phone, "body": text}

    async def send_message(self, phone: str, text: str) -> Dict[str, Any]:
        if not self.base_url or not self._token:
            raise ConfigurationError("WAPI_BASE_URL and WAPI_TOKEN are required for WHATSAPP_PROVIDER=wapi.")
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(self.base_url + self.send_path(), params={"token": self._token}, json=self.payload(phone, text),
                                 headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as e:
            raise ProviderError(str(e), user_message="The WAPI gateway could not be reached.") from e
        if r.status_code >= 400:
            raise ProviderError(r.text[:300], user_message=f"WAPI gateway error ({r.status_code}).")
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        return {"sent": True, "phone": phone, "provider": self.name, "response": body}

    async def health(self) -> Dict[str, Any]:
        ok = bool(self.base_url and self._token)
        return {"ok": ok, "detail": self.base_url if ok else "missing base url/token"}


def build_messaging_provider(settings: Settings, browser_service=None) -> MessagingProvider:
    p = settings.whatsapp_provider
    if p == WhatsAppProviderName.WEB:
        if browser_service is None:
            raise ConfigurationError("WhatsApp Web provider needs the browser service")
        return WhatsAppWebProvider(browser_service)
    if p == WhatsAppProviderName.BUSINESS:
        return WhatsAppBusinessProvider(settings.whatsapp_business_token, settings.whatsapp_business_phone_id)
    if p == WhatsAppProviderName.WAPI:
        return WAPIProvider(settings.wapi_base_url, settings.wapi_token, settings.wapi_instance_id)
    return DisabledMessaging()
