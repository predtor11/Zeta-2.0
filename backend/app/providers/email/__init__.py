"""Email providers.

    EmailProvider
    ├── IMAPSMTPProvider   generic IMAP/SMTP
    ├── GmailProvider      IMAP/SMTP preset (use an App Password)
    └── OutlookProvider    IMAP/SMTP preset (Outlook.com / Office 365 with app password or basic auth)

Authentication: app password (default) or OAuth 2.0 / XOAUTH2 (EMAIL_AUTH=oauth,
see `oauth.py`) for Gmail and Outlook.
"""

from __future__ import annotations

import asyncio
import base64
import email
import imaplib
import logging
import re
import smtplib
from abc import ABC, abstractmethod
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import EmailProviderName, Settings
from app.core.exceptions import ConfigurationError, ProviderError
from app.providers.email.oauth import OAuthManager, xoauth2_string

log = logging.getLogger(__name__)


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _body_text(msg: email.message.Message, max_chars: int = 20000) -> str:
    text, html = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunk = payload.decode(charset, errors="ignore")
        if ctype == "text/plain" and not text:
            text = chunk
        elif ctype == "text/html" and not html:
            html = chunk
    if not text and html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


class EmailProvider(ABC):
    name = "base"

    @abstractmethod
    async def list_messages(self, folder: str = "INBOX", limit: int = 20, unread_only: bool = False, query: str = "",
                            sender: str = "") -> List[Dict[str, Any]]: ...

    @abstractmethod
    async def get_message(self, uid: str, folder: str = "INBOX") -> Dict[str, Any]: ...

    @abstractmethod
    async def send(self, to: List[str], subject: str, body: str, cc: Optional[List[str]] = None,
                   attachments: Optional[List[Path]] = None) -> Dict[str, Any]: ...

    @abstractmethod
    async def download_attachment(self, uid: str, filename: str, dest_dir: Path, folder: str = "INBOX") -> Path: ...

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "detail": self.name}


class DisabledEmail(EmailProvider):
    name = "disabled"

    def _raise(self):
        raise ConfigurationError("Email is not configured. Set EMAIL_PROVIDER=gmail|outlook|imap with EMAIL_ADDRESS and EMAIL_PASSWORD (app password).")

    async def list_messages(self, *a, **k):
        self._raise()

    async def get_message(self, *a, **k):
        self._raise()

    async def send(self, *a, **k):
        self._raise()

    async def download_attachment(self, *a, **k):
        self._raise()

    async def health(self) -> Dict[str, Any]:
        return {"ok": False, "detail": "disabled"}


class IMAPSMTPProvider(EmailProvider):
    name = "imap"

    def __init__(self, address: str, password: str, imap_host: str, imap_port: int = 993, smtp_host: str = "", smtp_port: int = 587,
                 oauth: Optional[OAuthManager] = None, oauth_provider: str = ""):
        self.address = address
        self._password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.oauth = oauth
        self.oauth_provider = oauth_provider

    @property
    def uses_oauth(self) -> bool:
        return self.oauth is not None and bool(self.oauth_provider)

    async def _credential(self) -> Tuple[str, str]:
        """(login name, secret) - secret is an app password or a fresh OAuth access token."""
        if self.uses_oauth:
            token = await self.oauth.access_token(self.oauth_provider)  # type: ignore[union-attr]
            address = self.address or self.oauth.email_for(self.oauth_provider)  # type: ignore[union-attr]
            if not address:
                raise ConfigurationError("EMAIL_ADDRESS is required (the OAuth provider did not return your address).")
            return address, token
        if not (self.address and self._password):
            raise ConfigurationError("EMAIL_ADDRESS and EMAIL_PASSWORD are required (or set EMAIL_AUTH=oauth and connect the account).")
        return self.address, self._password

    def _imap(self, cred: Tuple[str, str]) -> imaplib.IMAP4_SSL:
        if not self.imap_host:
            raise ConfigurationError("EMAIL_IMAP_HOST is required.")
        address, secret = cred
        try:
            m = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
            if self.uses_oauth:
                m.authenticate("XOAUTH2", lambda _: xoauth2_string(address, secret).encode())
            else:
                m.login(address, secret)
            return m
        except imaplib.IMAP4.error as e:
            hint = "Reconnect the account in Settings > Integrations." if self.uses_oauth else "For Gmail/Outlook use an App Password."
            raise ProviderError(str(e), user_message=f"Email login failed. {hint}") from e
        except OSError as e:
            raise ProviderError(str(e), user_message=f"Could not connect to the mail server {self.imap_host}.") from e

    def _list_sync(self, cred: Tuple[str, str], folder: str, limit: int, unread_only: bool, query: str, sender: str) -> List[Dict[str, Any]]:
        m = self._imap(cred)
        try:
            status, _ = m.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise ProviderError(f"folder {folder} not found", user_message=f"Mail folder '{folder}' was not found.")
            criteria = []
            if unread_only:
                criteria.append("UNSEEN")
            if sender:
                criteria += ["FROM", f'"{sender}"']
            if query:
                criteria += ["OR", "SUBJECT", f'"{query}"', "BODY", f'"{query}"']
            status, data = m.uid("search", None, *(criteria or ["ALL"]))
            uids = data[0].split() if data and data[0] else []
            uids = uids[-limit:][::-1]
            out = []
            for uid in uids:
                status, msgdata = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)] FLAGS)")
                if status != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                flags = msgdata[0][0].decode(errors="ignore") if isinstance(msgdata[0][0], bytes) else ""
                try:
                    date = parsedate_to_datetime(msg.get("Date", "")).isoformat(timespec="minutes")
                except Exception:  # noqa: BLE001
                    date = msg.get("Date", "")
                name, addr = parseaddr(_decode(msg.get("From")))
                out.append({"uid": uid.decode(), "from": f"{name} <{addr}>" if name else addr, "to": _decode(msg.get("To")),
                            "subject": _decode(msg.get("Subject")), "date": date, "unread": "\\Seen" not in flags})
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _get_sync(self, cred: Tuple[str, str], uid: str, folder: str) -> Dict[str, Any]:
        m = self._imap(cred)
        try:
            m.select(f'"{folder}"', readonly=True)
            status, data = m.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                raise ProviderError("not found", user_message=f"Email {uid} was not found.")
            msg = email.message_from_bytes(data[0][1])
            attachments = [{"filename": _decode(p.get_filename()), "size": len(p.get_payload(decode=True) or b""), "type": p.get_content_type()}
                           for p in msg.walk() if p.get_content_disposition() == "attachment"]
            return {"uid": uid, "from": _decode(msg.get("From")), "to": _decode(msg.get("To")), "cc": _decode(msg.get("Cc")),
                    "subject": _decode(msg.get("Subject")), "date": msg.get("Date", ""), "body": _body_text(msg), "attachments": attachments}
        finally:
            m.logout()

    def _download_sync(self, cred: Tuple[str, str], uid: str, filename: str, dest_dir: Path, folder: str) -> Path:
        m = self._imap(cred)
        try:
            m.select(f'"{folder}"', readonly=True)
            status, data = m.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                raise ProviderError("not found", user_message=f"Email {uid} was not found.")
            msg = email.message_from_bytes(data[0][1])
            for p in msg.walk():
                if p.get_content_disposition() == "attachment" and _decode(p.get_filename()) == filename:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r'[<>:"/\\|?*]', "_", filename)
                    out = dest_dir / safe
                    out.write_bytes(p.get_payload(decode=True) or b"")
                    return out
            raise ProviderError("attachment missing", user_message=f"Attachment '{filename}' not found in that email.")
        finally:
            m.logout()

    def _send_sync(self, cred: Tuple[str, str], to: List[str], subject: str, body: str, cc: Optional[List[str]], attachments: Optional[List[Path]]) -> Dict[str, Any]:
        if not self.smtp_host:
            raise ConfigurationError("EMAIL_SMTP_HOST is required to send.")
        address, secret = cred
        msg = EmailMessage()
        msg["From"] = address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        for path in attachments or []:
            import mimetypes

            ctype, _ = mimetypes.guess_type(str(path))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                if self.uses_oauth:
                    code, resp = s.docmd("AUTH", "XOAUTH2 " + base64.b64encode(xoauth2_string(address, secret).encode()).decode())
                    if code != 235:
                        raise smtplib.SMTPAuthenticationError(code, resp)
                else:
                    s.login(address, secret)
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise ProviderError(str(e), user_message="SMTP login failed. Use an App Password.") from e
        except (smtplib.SMTPException, OSError) as e:
            raise ProviderError(str(e), user_message=f"Sending failed: {e.__class__.__name__}.") from e
        return {"sent": True, "to": to, "subject": subject}

    async def list_messages(self, folder="INBOX", limit=20, unread_only=False, query="", sender="") -> List[Dict[str, Any]]:
        cred = await self._credential()
        return await asyncio.get_running_loop().run_in_executor(None, self._list_sync, cred, folder, limit, unread_only, query, sender)

    async def get_message(self, uid: str, folder: str = "INBOX") -> Dict[str, Any]:
        cred = await self._credential()
        return await asyncio.get_running_loop().run_in_executor(None, self._get_sync, cred, uid, folder)

    async def send(self, to, subject, body, cc=None, attachments=None) -> Dict[str, Any]:
        cred = await self._credential()
        return await asyncio.get_running_loop().run_in_executor(None, self._send_sync, cred, to, subject, body, cc, attachments)

    async def download_attachment(self, uid: str, filename: str, dest_dir: Path, folder: str = "INBOX") -> Path:
        cred = await self._credential()
        return await asyncio.get_running_loop().run_in_executor(None, self._download_sync, cred, uid, filename, dest_dir, folder)

    async def health(self) -> Dict[str, Any]:
        if self.uses_oauth:
            st = self.oauth.status(self.oauth_provider)  # type: ignore[union-attr]
            return {"ok": st["connected"], "detail": f"{self.name} via OAuth: {st['email'] or ('connected' if st['connected'] else 'not connected')}"}
        ok = bool(self.address and self._password and self.imap_host)
        return {"ok": ok, "detail": f"{self.name}: {self.address or 'no address'} @ {self.imap_host or 'no host'}"}


class GmailProvider(IMAPSMTPProvider):
    name = "gmail"

    def __init__(self, address: str, password: str, oauth: Optional[OAuthManager] = None):
        super().__init__(address, password, "imap.gmail.com", 993, "smtp.gmail.com", 587,
                         oauth=oauth, oauth_provider="gmail" if oauth else "")


class OutlookProvider(IMAPSMTPProvider):
    name = "outlook"

    def __init__(self, address: str, password: str, oauth: Optional[OAuthManager] = None):
        super().__init__(address, password, "outlook.office365.com", 993, "smtp.office365.com", 587,
                         oauth=oauth, oauth_provider="outlook" if oauth else "")


def build_oauth_manager(settings: Settings) -> OAuthManager:
    return OAuthManager(settings.data_dir / "oauth_tokens.json", google_client_id=settings.google_client_id,
                        google_client_secret=settings.google_client_secret, microsoft_client_id=settings.microsoft_client_id,
                        microsoft_tenant=settings.microsoft_tenant,
                        redirect_uri=f"http://localhost:{settings.port}/api/email/oauth/callback")


def build_email_provider(settings: Settings, oauth: Optional[OAuthManager] = None) -> EmailProvider:
    p = settings.email_provider
    use_oauth = settings.email_auth.lower() == "oauth"
    if p == EmailProviderName.GMAIL:
        return GmailProvider(settings.email_address, settings.email_password, oauth if use_oauth else None)
    if p == EmailProviderName.OUTLOOK:
        return OutlookProvider(settings.email_address, settings.email_password, oauth if use_oauth else None)
    if p == EmailProviderName.IMAP:
        return IMAPSMTPProvider(settings.email_address, settings.email_password, settings.email_imap_host, settings.email_imap_port,
                                settings.email_smtp_host, settings.email_smtp_port)
    return DisabledEmail()
