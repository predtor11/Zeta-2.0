"""Streaming replies, wake word matching/service, database URL handling, audit + new API routes."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import describe_database_url, normalize_database_url
from app.core.events import event_bus
from app.providers.llm.anthropic import AnthropicProvider
from app.providers.llm.ollama import OllamaProvider, StreamAccumulator, visible_text
from app.providers.llm.openai_compat import OpenAICompatibleProvider
from app.tasks.manager import TaskStatus
from app.voice import wakeword
from app.voice.wakeword import WakeWordService, phrase_matches


@pytest_asyncio.fixture()
async def client(svc):
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ----------------------------------------------------------------- streaming helpers
def test_visible_text_hides_think_blocks():
    assert visible_text("<think>plan</think>Hello") == "Hello"
    assert visible_text("Hello <think>unfinished") == "Hello "
    assert visible_text("plain") == "plain"


def test_stream_accumulator_emits_only_visible_deltas():
    acc = StreamAccumulator()
    out = "".join(acc.push(c) for c in ["<thi", "nk>secret", "</think>", "Hel", "lo ", "<think>x</think>", "world"])
    assert out == "Hello world"
    assert acc.raw.endswith("world")


@pytest.mark.asyncio
async def test_ollama_streams_content_and_tool_calls():
    lines = [
        {"message": {"role": "assistant", "content": "<think>hmm</think>"}, "done": False},
        {"message": {"role": "assistant", "content": "Sure, "}, "done": False},
        {"message": {"role": "assistant", "content": "checking."}, "done": False},
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "git_status", "arguments": {"path": "."}}}]}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())

    p = OllamaProvider("qwen3:8b")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url)
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await p.chat_stream([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "git_status"}}], on_delta=on_delta)
    assert "".join(deltas) == "Sure, checking."
    assert resp.content == "Sure, checking." and resp.tool_calls[0].name == "git_status" and resp.tool_calls[0].arguments == {"path": "."}
    assert resp.usage["completion_tokens"] == 5


@pytest.mark.asyncio
async def test_openai_compat_streams_sse_with_tool_fragments():
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "search_files", "arguments": "{\"qu"}}]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ery\": \"pdf\"}"}}]}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    p = OpenAICompatibleProvider("m", "http://test/v1", "k")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test/v1")
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await p.chat_stream([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "search_files"}}], on_delta=on_delta)
    assert "".join(deltas) == "Hello" and resp.content == "Hello"
    assert resp.tool_calls[0].id == "call_a" and resp.tool_calls[0].arguments == {"query": "pdf"}
    assert resp.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_anthropic_streams_text_and_tool_use():
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "look."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "list_directory"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"Documents\"}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    p = AnthropicProvider("claude-sonnet-5", api_key="k")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=p.base_url)
    deltas = []

    async def on_delta(t):
        deltas.append(t)

    resp = await p.chat_stream([{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "list_directory"}}], on_delta=on_delta)
    assert "".join(deltas) == "Let me look." and resp.content == "Let me look."
    assert resp.tool_calls[0].id == "toolu_1" and resp.tool_calls[0].arguments == {"path": "Documents"}
    assert resp.finish_reason == "tool_use" and resp.usage == {"prompt_tokens": 7, "completion_tokens": 12}


@pytest.mark.asyncio
async def test_orchestrator_publishes_deltas_then_final(svc):
    q = event_bus.subscribe()
    try:
        svc.llm.queue_text("one two three four five six seven")
        task = await svc.submit("stream please", wait=True)
        assert task.status == TaskStatus.COMPLETED
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        event_bus.unsubscribe(q)
    deltas = [e for e in events if e["type"] == "assistant_delta"]
    assert len(deltas) >= 2 and "".join(d["delta"] for d in deltas) == "one two three four five six seven"
    assert all(d["task_id"] == task.id for d in deltas)
    ends = [e for e in events if e["type"] == "assistant_stream_end"]
    assert ends and ends[-1]["discard"] is False
    final = [e for e in events if e["type"] == "assistant_message"]
    assert final[-1]["content"] == "one two three four five six seven"


@pytest.mark.asyncio
async def test_streaming_can_be_disabled(svc):
    svc.settings.llm_stream = False
    q = event_bus.subscribe()
    try:
        svc.llm.queue_text("no stream")
        await svc.submit("x", wait=True)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        event_bus.unsubscribe(q)
        svc.settings.llm_stream = True
    assert not [e for e in events if e["type"] == "assistant_delta"]
    assert [e for e in events if e["type"] == "assistant_message"]


# ----------------------------------------------------------------- wake word
@pytest.mark.parametrize("text,expected", [
    ("Hey Zeta", True), ("hey zita, open chrome", True), ("Zeta?", True), ("Okay Zetta what's up", True),
    ("hey there", False), ("the weather is nice", False), ("beta testing", False), ("", False),
])
def test_phrase_matches(text, expected):
    assert phrase_matches(text, "hey zeta") is expected


def test_wake_service_detection_cooldown_and_callback():
    hits = []
    svc = WakeWordService(enabled=True, phrase="hey zeta", on_wake=lambda eng, txt: hits.append((eng, txt)), cooldown_seconds=60, beep=False)
    svc._detected("hey zeta")
    svc._detected("hey zeta again")  # inside cooldown -> ignored
    assert hits == [("auto", "hey zeta")] and svc.detections == 1
    st = svc.status()
    assert st["enabled"] and not st["running"] and st["phrase"] == "hey zeta" and st["last_text"] == "hey zeta"


# The numbers below are measured, not chosen: 25 s of this laptop's microphone array in an empty
# room gave a median frame of 203, p90 232 and a loudest frame of 356, and a synthesized "Hey Zeta"
# pushed through the engine is recognised down to about RMS 400.
# 80 ms frame levels of a synthesized "hey zeta" scaled to an overall RMS of 400, loudest first.
ROOM_FLOOR, ROOM_PEAK = 205.0, 356.0
QUIET_PHRASE = [1050.0, 731.0, 699.0, 616.0, 521.0, 402.0]


def test_the_gate_lets_a_quietly_spoken_phrase_through():
    """The failure this fixes: detections=0 AND rejected=0 for eighty minutes. Nothing was being
    rejected because nothing got as far as being judged - the old gate stood at 615, which four
    of these frames clear when the engine wants five, so the phrase was dropped before Whisper,
    the filters and the matcher ever saw it, and no counter anywhere moved."""
    gate = wakeword.gate_rms(0.5, ROOM_FLOOR)
    assert len([f for f in QUIET_PHRASE if f > gate]) >= 5
    assert len([f for f in QUIET_PHRASE if f > 615.0]) < 5      # what it used to be


def test_the_gate_still_sits_above_an_empty_room():
    """It is a CPU guard: if room noise crosses it, the tiny model runs continuously for nothing."""
    assert wakeword.gate_rms(0.5, ROOM_FLOOR) > ROOM_PEAK


def test_turning_the_dial_up_lowers_the_bar_and_down_raises_it():
    quiet_room = [wakeword.gate_rms(s, ROOM_FLOOR) for s in (0.0, 0.5, 1.0)]
    assert quiet_room == sorted(quiet_room, reverse=True)
    # ...and in a noisy room the gate follows the room rather than a constant
    assert wakeword.gate_rms(0.5, 900.0) > wakeword.gate_rms(0.5, ROOM_FLOOR)


def test_the_status_says_where_an_utterance_stopped():
    """Read left to right it separates a dead microphone from a gate nothing crosses, from a VAD
    that hears nothing, from filters that are too strict - which the old status could not."""
    st = WakeWordService(enabled=True, phrase="hey zeta").status()
    for key in ("frames", "level", "gate", "noise_floor", "heard", "too_short", "empty", "rejected"):
        assert key in st, key


def test_wake_service_disabled_does_not_start():
    svc = WakeWordService(enabled=False)
    svc.start()
    assert not svc.running and svc.status()["enabled"] is False


@pytest.mark.asyncio
async def test_wake_event_reaches_ui_and_routes(client, svc):
    q = event_bus.subscribe()
    try:
        r = await client.post("/api/voice/wake/test")
        assert r.status_code == 200
        await asyncio.sleep(0)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        event_bus.unsubscribe(q)
    assert any(e["type"] == "wake_word" and e["phrase"] == "hey zeta" for e in events)
    r = await client.get("/api/voice/wake")
    body = r.json()
    assert body["enabled"] is False and "devices" in body
    r = await client.post("/api/voice/wake/pause?seconds=2")
    assert r.json()["paused_for"] == 2


# ----------------------------------------------------------------- database
def test_database_url_normalisation():
    url, kw = normalize_database_url("sqlite:///D:/x/zeta.db")
    assert url.startswith("sqlite+aiosqlite:///") and kw["connect_args"]["timeout"] == 30

    url, kw = normalize_database_url("postgresql://postgres.abc:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    assert url.startswith("postgresql+asyncpg://postgres.abc:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    assert kw["connect_args"]["ssl"] == "require" and kw["connect_args"]["statement_cache_size"] == 0 and kw["pool_pre_ping"]

    url, kw = normalize_database_url("postgres://u:p@db.abc.supabase.co:5432/postgres?sslmode=require")
    assert "sslmode" not in url and kw["connect_args"]["ssl"] == "require" and "statement_cache_size" not in kw["connect_args"]

    url, kw = normalize_database_url("postgresql://u:p@localhost:5432/zeta")
    assert "connect_args" not in kw  # local: no SSL forced

    d = describe_database_url("postgresql://u:secret@aws-0-x.pooler.supabase.com:6543/postgres")
    assert d["backend"] == "postgresql" and d["supabase"] and "secret" not in json.dumps(d)
    assert describe_database_url("sqlite:///D:/x/zeta.db")["backend"] == "sqlite"


@pytest.mark.asyncio
async def test_database_and_audit_routes(client, svc):
    r = await client.get("/api/system/database")
    body = r.json()
    assert body["backend"] == "sqlite" and body["ok"] is True
    svc.llm.queue_tool_call("get_system_info", {})
    svc.llm.queue_text("done")
    await svc.submit("system info", wait=True)
    await asyncio.sleep(0.05)
    r = await client.get("/api/audit?limit=20")
    rows = r.json()
    assert rows and {"event", "tool", "risk", "decision", "ts"} <= set(rows[0])
    assert any(x["event"] == "tool_call" and x["tool"] == "get_system_info" for x in rows)
    r = await client.get("/api/audit?event=user_request")
    assert all(x["event"] == "user_request" for x in r.json()) and r.json()
    r = await client.get("/api/system/status")
    assert r.json()["database"]["backend"] == "sqlite" and r.json()["voice"]["wake"]["enabled"] is False
