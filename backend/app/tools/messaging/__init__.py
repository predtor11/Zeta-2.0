"""Messaging tools: resolve contacts, send WhatsApp messages, read recent chats."""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from app.core.exceptions import ConfigurationError, ProviderError
from app.plugins.loader import Plugin
from app.providers.messaging import ContactBook, MessagingProvider
from app.security.permissions import RiskLevel
from app.security.validators import validate_phone
from app.tools.base import Tool, ToolContext, ToolResult


class ResolveContactArgs(BaseModel):
    query: str = Field(description="Name, alias or phone number")


class ResolveContactTool(Tool):
    name = "resolve_contact"
    description = ("Optional: look up a person in Zeta's local contact book (phone/email). NOT required before messaging: "
                   "send_whatsapp_message accepts a name and finds the chat in WhatsApp itself. Never search files or apps for contacts.")
    category = "messaging"
    risk_level = RiskLevel.READ_ONLY
    args_model = ResolveContactArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        book: ContactBook = ctx.service("contacts")
        provider: MessagingProvider = ctx.service("messaging")
        matches = book.resolve(args["query"])
        if not matches:
            if getattr(provider, "supports_name_lookup", False):
                return ToolResult.ok(
                    {"matches": [], "in_contact_book": False, "whatsapp_lookup_available": True,
                     "next_step": f"Call send_whatsapp_message with to='{args['query']}'; it will find the chat by name in WhatsApp."},
                    summary=f"'{args['query']}' not in the contact book; WhatsApp will search by name")
            return ToolResult.fail(f"No contact matching '{args['query']}' in the contact book. Ask the user for the number, or add it with add_contact.")
        return ToolResult.ok({"matches": [m.to_dict() for m in matches], "ambiguous": len(matches) > 1},
                             summary=f"{len(matches)} contact(s) found")


class AddContactArgs(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(default="", description="International format, e.g. +91 98765 43210")
    email: str = ""


class AddContactTool(Tool):
    name = "add_contact"
    description = "Add or update a contact (name, phone, email) in the local contact book."
    category = "messaging"
    risk_level = RiskLevel.SAFE
    args_model = AddContactArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        book: ContactBook = ctx.service("contacts")
        phone = validate_phone(args["phone"]) if args.get("phone") else ""
        c = book.add(args["name"], phone, args.get("email", ""))
        return ToolResult.ok(c.to_dict(), summary=f"Saved contact {c.name}")


class SendWhatsAppArgs(BaseModel):
    to: str = Field(description="Person's name (looked up in the contact book, then in WhatsApp's own chat list) or a phone number in international format")
    message: str = Field(min_length=1, max_length=4000)


class SendWhatsAppTool(Tool):
    name = "send_whatsapp_message"
    description = ("Send a WhatsApp message. Give the person's NAME as the user said it (or a phone number); the tool finds the "
                   "contact in the contact book or directly in WhatsApp's chat list. Call this directly - no lookup is needed first. "
                   "The recipient and text are shown to the user for confirmation before sending.")
    category = "messaging"
    risk_level = RiskLevel.SENSITIVE
    args_model = SendWhatsAppArgs
    timeout_seconds = 180
    log_arguments = False

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Send WhatsApp message to {args.get('to')}: \"{args.get('message', '')[:120]}\""

    def confirmation_details(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"recipient": args.get("to"), "message": args.get("message")}

    async def preview(self, args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        book: ContactBook = ctx.service("contacts")
        provider: MessagingProvider = ctx.service("messaging")
        matches = book.resolve(args["to"])
        if len(matches) == 1:
            return {"recipient": f"{matches[0].name} (+{matches[0].phone})",
                    "description": f"Send WhatsApp to {matches[0].name} (+{matches[0].phone}): \"{args['message'][:120]}\""}
        if not matches and getattr(provider, "supports_name_lookup", False):
            return {"recipient": f"{args['to']} (found via WhatsApp chat search)",
                    "description": f"Send WhatsApp to the chat named '{args['to']}': \"{args['message'][:120]}\""}
        return {}

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: MessagingProvider = ctx.service("messaging")
        book: ContactBook = ctx.service("contacts")
        matches = book.resolve(args["to"])
        if not matches:
            if getattr(provider, "supports_name_lookup", False):
                ctx.activity(f"Looking up '{args['to']}' in WhatsApp…")
                try:
                    result = await provider.send_to_name(args["to"], args["message"])
                except (ConfigurationError, ProviderError) as e:
                    return ToolResult.fail(e.user_message)
                return ToolResult.ok({**result, "recipient": result.get("chat") or args["to"]},
                                     summary=f"Sent WhatsApp to {result.get('chat') or args['to']}")
            return ToolResult.fail(f"I don't have a contact for '{args['to']}'. Ask the user for the phone number (or add it with add_contact).")
        if len(matches) > 1:
            return ToolResult.fail("Ambiguous contact: " + ", ".join(f"{m.name} (+{m.phone})" for m in matches) + ". Ask the user which one.")
        contact = matches[0]
        if not contact.phone:
            return ToolResult.fail(f"Contact {contact.name} has no phone number.")
        phone = validate_phone(contact.phone)
        ctx.activity(f"Sending WhatsApp to {contact.name}…")
        try:
            result = await provider.send_message(phone, args["message"])
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok({**result, "recipient": contact.name}, summary=f"Sent WhatsApp to {contact.name}")


class ReadWhatsAppArgs(BaseModel):
    chat: str = Field(description="Contact/chat name")
    limit: int = Field(default=20, ge=1, le=100)


class ReadWhatsAppTool(Tool):
    name = "read_whatsapp_messages"
    description = "Read recent messages from a WhatsApp chat (WhatsApp Web provider only)."
    category = "messaging"
    risk_level = RiskLevel.READ_ONLY
    args_model = ReadWhatsAppArgs
    timeout_seconds = 120

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        provider: MessagingProvider = ctx.service("messaging")
        try:
            msgs = await provider.read_recent(args["chat"], args.get("limit", 20))
        except (ConfigurationError, ProviderError) as e:
            return ToolResult.fail(e.user_message)
        return ToolResult.ok({"chat": args["chat"], "messages": msgs}, summary=f"{len(msgs)} messages", untrusted=True, source="whatsapp")


async def _health() -> Dict[str, Any]:
    return {"ok": True, "detail": "see WHATSAPP_PROVIDER"}


PLUGIN = Plugin(
    name="messaging",
    description="WhatsApp messaging via Web automation, Business API or a WAPI gateway",
    permissions=["messaging"],
    tools=lambda s: [ResolveContactTool(), AddContactTool(), SendWhatsAppTool(), ReadWhatsAppTool()],
    configuration={"WHATSAPP_PROVIDER": "web|business|wapi", "CONTACTS_FILE": "JSON contact book",
                   "WHATSAPP_BUSINESS_TOKEN": "Meta token", "WAPI_BASE_URL": "gateway URL"},
    health_check=_health,
)
