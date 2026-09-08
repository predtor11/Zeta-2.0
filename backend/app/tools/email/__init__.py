"""Email tools: list/search, read, summarize (via read), draft, send, download attachments."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.core.exceptions import ConfigurationError, ProviderError
from app.plugins.loader import Plugin
from app.providers.email import EmailProvider
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path, validate_email
from app.tools.base import Tool, ToolContext, ToolResult


class ListEmailArgs(BaseModel):
    folder: str = "INBOX"
    limit: int = Field(default=15, ge=1, le=100)
    unread_only: bool = False
    query: str = Field(default="", description="Words in subject/body")
    sender: str = Field(default="", description="Filter by sender name or address")


class ListEmailsTool(Tool):
    name = "list_emails"
    description = "List or search recent emails (uid, from, subject, date, unread). Use uid with read_email."
    category = "email"
    risk_level = RiskLevel.READ_ONLY
    args_model = ListEmailArgs
    timeout_seconds = 90

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: EmailProvider = ctx.service("email")
        ctx.activity("Checking email…")
        try:
            msgs = await provider.list_messages(args.get("folder", "INBOX"), args.get("limit", 15), args.get("unread_only", False),
                                                args.get("query", ""), args.get("sender", ""))
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok({"count": len(msgs), "messages": msgs}, summary=f"{len(msgs)} emails", untrusted=True, source="email")


class ReadEmailArgs(BaseModel):
    uid: str
    folder: str = "INBOX"


class ReadEmailTool(Tool):
    name = "read_email"
    description = "Read the full text of an email by uid (from list_emails), including attachment names. Summarize it yourself afterwards."
    category = "email"
    risk_level = RiskLevel.READ_ONLY
    args_model = ReadEmailArgs
    timeout_seconds = 90

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: EmailProvider = ctx.service("email")
        try:
            msg = await provider.get_message(args["uid"], args.get("folder", "INBOX"))
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok(msg, summary=f"Read email '{msg.get('subject', '')[:50]}'", untrusted=True, source="email")


class SendEmailArgs(BaseModel):
    to: List[str] = Field(min_length=1, description="Recipient addresses (or contact names)")
    subject: str = Field(max_length=300)
    body: str = Field(max_length=50000)
    cc: List[str] = []
    attachments: List[str] = Field(default_factory=list, description="Local file paths")


class SendEmailTool(Tool):
    name = "send_email"
    description = "Send an email. The full draft (recipients, subject, body) is shown to the user for confirmation first."
    category = "email"
    risk_level = RiskLevel.SENSITIVE
    args_model = SendEmailArgs
    timeout_seconds = 120
    log_arguments = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Send email to {', '.join(args.get('to', []))}: \"{args.get('subject', '')}\""

    def confirmation_details(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"to": args.get("to"), "cc": args.get("cc"), "subject": args.get("subject"), "body": args.get("body"),
                "attachments": args.get("attachments")}

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: EmailProvider = ctx.service("email")
        book = ctx.services.get("contacts")
        to: List[str] = []
        for r in args["to"]:
            if "@" in r:
                to.append(validate_email(r))
            elif book is not None:
                matches = [c for c in book.resolve(r) if c.email]
                if len(matches) != 1:
                    return ToolResult.fail(f"Could not resolve '{r}' to a single email address. Ask the user.")
                to.append(matches[0].email)
            else:
                return ToolResult.fail(f"'{r}' is not an email address.")
        cc = [validate_email(c) for c in args.get("cc", [])]
        atts = [resolve_path(p, ctx.settings.allowed_roots, must_exist=True) for p in args.get("attachments", [])]
        try:
            result = await provider.send(to, args["subject"], args["body"], cc, atts)
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok(result, summary=f"Email sent to {', '.join(to)}")


class DraftEmailArgs(BaseModel):
    to: List[str] = []
    subject: str = ""
    body: str = ""


class DraftEmailTool(Tool):
    name = "draft_email"
    description = "Prepare an email draft and show it to the user without sending (use before send_email when the user wants to review)."
    category = "email"
    risk_level = RiskLevel.READ_ONLY
    args_model = DraftEmailArgs
    log_arguments = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult.ok({"draft": args, "note": "Not sent. Call send_email to send after the user approves."}, summary="Draft prepared")


class DownloadAttachmentArgs(BaseModel):
    uid: str
    filename: str
    folder: str = "INBOX"
    save_to: str = Field(default="", description="Destination folder (default: Downloads)")


class DownloadAttachmentTool(Tool):
    name = "download_email_attachment"
    description = "Save an attachment from an email to disk."
    category = "email"
    risk_level = RiskLevel.SENSITIVE
    args_model = DownloadAttachmentArgs
    timeout_seconds = 180

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: EmailProvider = ctx.service("email")
        dest = resolve_path(args.get("save_to") or ctx.settings.browser_download_dir, ctx.settings.allowed_roots, for_write=True)
        try:
            path = await provider.download_attachment(args["uid"], args["filename"], dest, args.get("folder", "INBOX"))
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok({"path": str(path)}, summary=f"Saved {path.name}", artifacts={"path": str(path)})


PLUGIN = Plugin(
    name="email",
    description="Read, search, draft and send email via Gmail/Outlook/IMAP",
    permissions=["email"],
    tools=lambda s: [ListEmailsTool(), ReadEmailTool(), DraftEmailTool(), SendEmailTool(), DownloadAttachmentTool()],
    configuration={"EMAIL_PROVIDER": "gmail|outlook|imap", "EMAIL_ADDRESS": "account", "EMAIL_PASSWORD": "app password"},
)
