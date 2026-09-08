"""WebSocket: real-time task, activity, confirmation and message events."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import ws_auth
from app.core.events import event_bus
from app.services import get_services

router = APIRouter(tags=["ws"])
log = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    svc = get_services()
    if not await ws_auth(ws, svc):
        await ws.close(code=4401)
        return
    await ws.accept()
    queue = event_bus.subscribe()
    await ws.send_text(json.dumps({"type": "hello", "pending_confirmations": svc.confirmations.pending(),
                                   "active_tasks": [t.to_dict() for t in svc.tasks.active()]}, default=str))

    async def _sender():
        while True:
            ev = await queue.get()
            await ws.send_text(json.dumps(ev, default=str))

    sender = asyncio.ensure_future(_sender())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            if t == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif t == "confirm":
                svc.confirmations.resolve(msg.get("confirmation_id", ""), bool(msg.get("approved")), bool(msg.get("remember")))
            elif t == "cancel":
                if msg.get("task_id"):
                    svc.confirmations.cancel_for_task(msg["task_id"])
                    svc.tasks.cancel(msg["task_id"])
                else:
                    svc.confirmations.cancel_all()
                    svc.tasks.cancel_all()
            elif t == "chat" and msg.get("message"):
                await svc.submit(str(msg["message"])[:20000], msg.get("conversation_id"))
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("websocket closed: %s", e.__class__.__name__)
    finally:
        sender.cancel()
        event_bus.unsubscribe(queue)
