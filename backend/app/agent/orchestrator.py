"""The Zeta orchestrator: the agent loop.

    user message -> build context (system prompt + memory + history)
      -> LLM -> tool calls? -> validate -> PermissionManager -> (confirm?) -> execute -> audit
      -> feed results back -> ... -> final reply

Design notes
* Providers with native tool calling get OpenAI-format tool schemas; others use
  the JSON-in-text fallback protocol.
* Every tool result that carries external content is wrapped as UNTRUSTED.
* All state changes are published to the event bus for the UI.
* Cancellation: `asyncio.CancelledError` unwinds cleanly; pending confirmations
  for the task are released.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from app.agent.prompts import build_system_prompt, context_note
from app.agent.toolcall_fallback import parse_tool_calls, tools_text
from app.core.config import Settings
from app.core.events import event_bus
from app.core.exceptions import (ConfirmationDenied, ConfirmationTimeout, PermissionDenied, ProviderError,
                                 ToolTimeout, ToolValidationError, ZetaError)
from app.emotion.engine import EmotionEngine
from app.emotion.state import EmotionState
from app.memory.long_term import LongTermMemory
from app.memory.short_term import ShortTermMemory
from app.providers.llm.base import LLMProvider, LLMResponse, ToolCall
from app.security import audit
from app.security.confirmation import ConfirmationManager
from app.security.permissions import Decision, PermissionManager, RiskLevel
from app.security.secrets import SecretStore
from app.security.trust import detect_injection, wrap_untrusted
from app.tasks.manager import Task, TaskManager, TaskStatus
from app.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, *, settings: Settings, llm: LLMProvider, registry: ToolRegistry, permissions: PermissionManager,
                 confirmations: ConfirmationManager, tasks: TaskManager, short_term: ShortTermMemory,
                 long_term: LongTermMemory, secrets: SecretStore, services: Optional[Dict[str, Any]] = None,
                 emotion: Optional[EmotionEngine] = None, speech_note: str = ""):
        self.settings = settings
        self.llm = llm
        self.registry = registry
        self.permissions = permissions
        self.confirmations = confirmations
        self.tasks = tasks
        self.short_term = short_term
        self.long_term = long_term
        self.secrets = secrets
        self.services: Dict[str, Any] = services or {}
        self.emotion = emotion
        self.speech_note = speech_note

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def run(self, task: Task, message: str) -> str:
        """Run one user request to completion. Returns the final reply text."""
        started = time.monotonic()
        try:
            task.conversation_id = await self.short_term.ensure_conversation(task.conversation_id, title_hint=message)
            await self.short_term.append(task.conversation_id, "user", message, task_id=task.id)
            await audit.record("user_request", task_id=task.id, details={"message": message[:500]})
            self.tasks.set_status(task, TaskStatus.PLANNING, message="Thinking…")
            reply = await self._loop(task, message)
            self.tasks.set_status(task, TaskStatus.COMPLETED, result=reply, message="Done")
            return reply
        except asyncio.CancelledError:
            self.confirmations.cancel_for_task(task.id)
            if not task.status.terminal:
                self.tasks.set_status(task, TaskStatus.CANCELLED, error="cancelled", message="Task cancelled")
            await self._safe_append(task, "assistant", "Stopped.")
            return "Stopped."
        except ProviderError as e:
            msg = e.user_message
            log.error("Provider error in task %s: %s", task.id, e)
            self.tasks.set_status(task, TaskStatus.FAILED, result=msg, error=msg, message=f"Failed: {msg}")
            await self._safe_append(task, "assistant", msg)
            return msg
        except ZetaError as e:
            msg = e.user_message
            self.tasks.set_status(task, TaskStatus.FAILED, result=msg, error=msg, message=f"Failed: {msg}")
            await self._safe_append(task, "assistant", msg)
            return msg
        except Exception as e:  # noqa: BLE001
            log.exception("Unhandled error in task %s", task.id)
            msg = f"Something went wrong internally ({e.__class__.__name__}). Check the logs for details."
            self.tasks.set_status(task, TaskStatus.FAILED, result=msg, error=str(e), message=f"Failed: {e.__class__.__name__}")
            await self._safe_append(task, "assistant", msg)
            return msg
        finally:
            log.info("task %s finished in %d ms", task.id, int((time.monotonic() - started) * 1000),
                     extra={"task_id": task.id})

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------
    async def _loop(self, task: Task, message: str) -> str:
        cloud = not self.settings.is_local
        enabled_tools = self.registry.enabled(self.permissions, cloud=cloud)
        schemas = [t.schema() for t in enabled_tools]
        tool_names = {t.name for t in enabled_tools}
        native = bool(schemas) and self.llm.supports_tools

        memory_block = ""
        try:
            memory_block = await self.long_term.context_block(message)
        except Exception as e:  # noqa: BLE001
            log.warning("memory lookup failed: %s", e)

        emotion_note = ""
        if self.emotion and self.emotion.enabled:
            try:
                # A voice turn was already analysed with its audio; reuse it instead of re-reading the words alone.
                state: EmotionState = self.emotion.take_staged(message) or self.emotion.analyze_text(message)
                self.emotion.record(state, conversation_id=task.conversation_id, task_id=task.id)
                emotion_note = self.emotion.prompt_note(state, message)
                if state.confidence >= 0.3 or state.crisis:
                    event_bus.publish("emotion", task_id=task.id, conversation_id=task.conversation_id,
                                      emotion=state.to_dict(), trend=self.emotion.trend())
                if state.crisis:
                    await audit.record("emotion", task_id=task.id, details={"crisis": state.crisis.get("level"), "label": state.label})
            except Exception as e:  # noqa: BLE001
                log.warning("emotion analysis failed: %s", e)

        system_prompt = build_system_prompt(
            name=self.settings.zeta_name, tool_names=sorted(tool_names),
            allowed_roots=self.settings.allowed_roots, mode=self.settings.zeta_mode.value,
            fallback_tools_text="" if native else tools_text(schemas),
            emotional=bool(self.emotion and self.emotion.enabled), speech_note=self.speech_note,
            support_mode=bool(self.emotion and self.emotion.support_mode),
            languages=self.settings.stt_languages,
        )
        history = await self.short_term.history(task.conversation_id, self.settings.agent_history_messages)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}, *history]
        if not history or history[-1].get("role") != "user":
            messages.append({"role": "user", "content": message})
        # Time + memories ride on the last user turn (see prompts.context_note) so the big static prefix stays cached.
        last = messages[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), str):
            messages[-1] = {**last, "content": last["content"] + context_note(memory_block, emotion_note)}

        for step in range(self.settings.agent_max_steps):
            self.tasks.set_status(task, TaskStatus.EXECUTING if step else TaskStatus.PLANNING,
                                  message="Thinking…" if step == 0 else "Working…")
            response = await self._call_llm(task, step, messages, schemas if native else None, stream=native)
            tool_calls = response.tool_calls
            content = response.content or ""
            if not tool_calls and not native:
                tool_calls = parse_tool_calls(content, tool_names)
                if tool_calls:
                    content = ""  # the JSON was the call, not user-facing text
            if tool_calls and native:
                # Anything streamed so far was preamble to a tool call; the UI shows it as interim text.
                event_bus.publish("assistant_stream_end", task_id=task.id, conversation_id=task.conversation_id,
                                  step=step, discard=not content.strip())

            if not tool_calls:
                reply = content.strip() or "(no response)"
                await self.short_term.append(task.conversation_id, "assistant", reply, task_id=task.id)
                event_bus.publish("assistant_message", task_id=task.id, conversation_id=task.conversation_id, content=reply)
                return reply

            # Persist the assistant turn that requested tools
            if native:
                assistant_msg = response.to_message()
                await self.short_term.append(task.conversation_id, "assistant", content,
                                             tool_calls=assistant_msg.get("tool_calls"), task_id=task.id)
            else:
                assistant_msg = {"role": "assistant", "content": json.dumps({"tool": tool_calls[0].name, "arguments": tool_calls[0].arguments})}
                await self.short_term.append(task.conversation_id, "assistant", assistant_msg["content"], task_id=task.id)
            messages.append(assistant_msg)
            if content.strip():
                event_bus.publish("assistant_message", task_id=task.id, conversation_id=task.conversation_id,
                                  content=content.strip(), interim=True)

            for tc in tool_calls:
                result = await self._execute_tool(task, tc)
                text = result.to_llm_text(self.settings.tool_output_max_chars)
                if result.untrusted:
                    text = wrap_untrusted(text, result.source or tc.name)
                if native:
                    tool_msg = {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": text}
                    await self.short_term.append(task.conversation_id, "tool", text, tool_call_id=tc.id,
                                                 tool_name=tc.name, task_id=task.id)
                else:
                    tool_msg = {"role": "user", "content": f"[Tool result for {tc.name}]\n{text}"}
                    await self.short_term.append(task.conversation_id, "user", tool_msg["content"], tool_name=tc.name, task_id=task.id)
                messages.append(tool_msg)

        # Step budget exhausted
        reply = "I stopped because the task needed more steps than I'm allowed. Here is where I got to: " + \
                (task.plan[-1]["description"] if task.plan else "the task is incomplete.")
        await self.short_term.append(task.conversation_id, "assistant", reply, task_id=task.id)
        event_bus.publish("assistant_message", task_id=task.id, conversation_id=task.conversation_id, content=reply)
        return reply

    async def _call_llm(self, task: Task, step: int, messages: List[Dict[str, Any]],
                        schemas: Optional[List[Dict[str, Any]]], *, stream: bool) -> LLMResponse:
        """One model call. With streaming on, visible text is pushed to the UI as `assistant_delta` events."""
        stream_fn = getattr(self.llm, "chat_stream", None)
        if not (stream and self.settings.llm_stream and stream_fn):
            return await self.llm.chat(messages, schemas)
        seq = 0

        async def on_delta(text: str) -> None:
            nonlocal seq
            seq += 1
            event_bus.publish("assistant_delta", task_id=task.id, conversation_id=task.conversation_id, step=step, seq=seq, delta=text)

        try:
            return await stream_fn(messages, schemas, on_delta=on_delta)
        finally:
            if seq:
                event_bus.publish("assistant_stream_end", task_id=task.id, conversation_id=task.conversation_id, step=step, discard=False)

    # ------------------------------------------------------------------
    # Tool execution with permission + confirmation + audit
    # ------------------------------------------------------------------
    def _context(self, task: Task) -> ToolContext:
        def emit(message: str, **extra: Any) -> None:
            event_bus.activity(message, task_id=task.id, **extra)

        return ToolContext(settings=self.settings, secrets=self.secrets, permissions=self.permissions,
                           task_id=task.id, conversation_id=task.conversation_id, services=self.services, emit=emit)

    async def _execute_tool(self, task: Task, tc: ToolCall) -> ToolResult:
        tool = self.registry.get(tc.name)
        if tool is None:
            event_bus.activity(f"Unknown tool requested: {tc.name}", task_id=task.id, level="warning")
            return ToolResult.fail(f"Unknown tool '{tc.name}'. Use only the tools listed.")
        if not self.settings.is_local and not tool.available_in_cloud:
            return ToolResult.fail(f"Tool '{tc.name}' is not available in cloud mode (it needs the host computer).")

        # 1. Validate arguments
        try:
            args = tool.validate(tc.arguments)
        except ToolValidationError as e:
            await audit.record("tool_call", task_id=task.id, tool=tool.name, success=False, details={"error": str(e)})
            event_bus.activity(f"Invalid arguments for {tool.name}", task_id=task.id, level="warning")
            return ToolResult.fail(str(e))

        # 2. Classify + permission decision
        try:
            risk = tool.classify(args)
        except ZetaError as e:
            return ToolResult.fail(e.user_message)
        action_key = tool.action_key(args)
        perm = self.permissions.check(tool.name, tool.category, risk, requires_confirmation=tool.requires_confirmation,
                                      action_key=action_key)
        await audit.record("permission", task_id=task.id, tool=tool.name, risk=risk.value, decision=perm.decision.value,
                           details={"reason": perm.reason, **({"args": args} if tool.log_arguments else {})})
        description = tool.describe(args)

        if perm.decision == Decision.DENY:
            event_bus.activity(f"Denied: {description} ({perm.reason})", task_id=task.id, level="warning", tool=tool.name)
            return ToolResult.fail(f"Denied by permission policy: {perm.reason}. Do not retry this action; tell the user.")

        # 3. Confirmation
        if perm.decision == Decision.CONFIRM:
            ctx = self._context(task)
            details = tool.confirmation_details(args)
            try:
                preview = await asyncio.wait_for(tool.preview(args, ctx), timeout=30)
                if preview:
                    details.update(preview)
                    if preview.get("description"):
                        description = preview["description"]
            except Exception as e:  # noqa: BLE001
                log.debug("preview failed for %s: %s", tool.name, e)
            self.tasks.set_status(task, TaskStatus.WAITING_FOR_CONFIRMATION, message=f"Needs confirmation: {description}")
            try:
                remember = await self.confirmations.request(task_id=task.id, tool=tool.name, risk=risk.value,
                                                            description=description, details=details, action_key=action_key)
                if remember:
                    self.permissions.remember_allow(action_key)
                await audit.record("confirmation", task_id=task.id, tool=tool.name, risk=risk.value, decision="approved")
            except ConfirmationDenied:
                await audit.record("confirmation", task_id=task.id, tool=tool.name, risk=risk.value, decision="declined")
                self.tasks.set_status(task, TaskStatus.EXECUTING, message="Confirmation declined")
                return ToolResult.fail("The user declined to confirm this action. Do not retry it; acknowledge and stop this step.")
            except ConfirmationTimeout:
                await audit.record("confirmation", task_id=task.id, tool=tool.name, risk=risk.value, decision="timeout")
                self.tasks.set_status(task, TaskStatus.EXECUTING, message="Confirmation timed out")
                return ToolResult.fail("The confirmation timed out; the action was NOT performed.")
            self.tasks.set_status(task, TaskStatus.EXECUTING, message="Confirmed")

        # 4. Execute
        ctx = self._context(task)
        self.tasks.add_tool_used(task, tool.name)
        event_bus.publish("tool_start", task_id=task.id, tool=tool.name, message=description, risk=risk.value,
                          args=args if tool.log_arguments else {"_redacted": True})
        started = time.monotonic()
        timeout = tool.timeout_seconds or self.settings.tool_timeout_seconds
        try:
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            result = ToolResult.fail(f"{tool.name} timed out after {timeout}s")
        except asyncio.CancelledError:
            raise
        except PermissionDenied as e:
            result = ToolResult.fail(e.user_message)
        except ToolTimeout as e:
            result = ToolResult.fail(e.user_message)
        except ZetaError as e:
            result = ToolResult.fail(e.user_message)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s crashed", tool.name)
            result = ToolResult.fail(f"{tool.name} failed: {e.__class__.__name__}: {e}")
        duration = int((time.monotonic() - started) * 1000)

        if result.untrusted:
            suspicious, hits = detect_injection(result.to_llm_text(50000))
            if suspicious:
                event_bus.activity(f"Warning: external content from {tool.name} contains instruction-like text; ignoring it.",
                                   task_id=task.id, level="warning", hits=hits)

        summary = result.summary or (f"{tool.name} succeeded" if result.success else result.error or "failed")
        event_bus.publish("tool_end", task_id=task.id, tool=tool.name, success=result.success, message=summary,
                          duration_ms=duration, artifacts=result.artifacts)
        await audit.record("tool_call", task_id=task.id, tool=tool.name, risk=risk.value, success=result.success,
                           duration_ms=duration, details={"summary": summary[:500]})
        return result

    # ------------------------------------------------------------------
    async def _safe_append(self, task: Task, role: str, content: str) -> None:
        try:
            if task.conversation_id:
                await self.short_term.append(task.conversation_id, role, content, task_id=task.id)
                event_bus.publish("assistant_message", task_id=task.id, conversation_id=task.conversation_id, content=content)
        except Exception:  # noqa: BLE001
            log.debug("could not append message", exc_info=True)

    async def one_shot(self, prompt: str, *, system: str = "", model: Optional[str] = None,
                       images: Optional[List[Dict[str, Any]]] = None) -> str:
        """Tool-free LLM call used by summarisation / vision helpers."""
        content: Any = prompt
        if images:
            content = [{"type": "text", "text": prompt}, *images]
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        resp: LLMResponse = await self.llm.chat(msgs, None, model=model)
        return resp.content
