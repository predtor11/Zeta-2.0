"""HTTP API tests against the running app with the mock provider."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture()
async def client(svc):
    from app.main import create_app

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_ping_and_status(client):
    r = await client.get("/api/ping")
    assert r.status_code == 200 and r.json()["ok"]
    r = await client.get("/api/system/status")
    body = r.json()
    assert body["llm"]["provider"] == "mock" and body["tools"] > 10
    assert any(p["name"] == "filesystem" for p in body["plugins"])


@pytest.mark.asyncio
async def test_chat_wait(client, svc):
    svc.llm.queue_text("Hi from Zeta")
    r = await client.post("/api/chat", json={"message": "hello", "wait": True})
    body = r.json()
    assert body["status"] == "COMPLETED" and body["reply"] == "Hi from Zeta"
    r = await client.get(f"/api/conversations/{body['conversation_id']}/messages")
    assert [m["role"] for m in r.json()] == ["user", "assistant"]
    r = await client.get("/api/tasks")
    assert r.json()[0]["id"] == body["task_id"]


@pytest.mark.asyncio
async def test_confirm_endpoint(client, svc, sandbox):
    import asyncio

    target = sandbox / "Downloads" / "big.bin"
    svc.llm.queue_tool_call("delete_files", {"paths": [str(target)], "permanent": True})
    svc.llm.queue_text("Deleted.")
    r = await client.post("/api/chat", json={"message": "delete big.bin"})
    task_id = r.json()["task_id"]
    for _ in range(100):
        pend = (await client.get("/api/confirmations")).json()
        if pend:
            break
        await asyncio.sleep(0.05)
    assert pend[0]["task_id"] == task_id
    r = await client.post("/api/confirm", json={"confirmation_id": pend[0]["id"], "approved": True})
    assert r.status_code == 200
    await svc.tasks.wait(svc.tasks.get(task_id), timeout=10)
    assert (await client.get(f"/api/tasks/{task_id}")).json()["status"] == "COMPLETED"
    assert not target.exists()
    r = await client.post("/api/confirm", json={"confirmation_id": "nope", "approved": True})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_memory_endpoints(client):
    r = await client.post("/api/memory", json={"content": "User likes dark mode", "category": "preference"})
    mid = r.json()["id"]
    r = await client.get("/api/memory", params={"q": "dark mode"})
    assert r.json()[0]["id"] == mid
    r = await client.delete(f"/api/memory/{mid}")
    assert r.json()["deleted"]
    assert (await client.delete(f"/api/memory/{mid}")).status_code == 404


@pytest.mark.asyncio
async def test_tools_permissions_activity(client):
    tools = (await client.get("/api/tools")).json()
    names = {t["name"] for t in tools}
    assert {"search_files", "open_file", "launch_application", "execute_command", "web_search", "send_whatsapp_message"} <= names
    r = await client.put("/api/permissions", json={"enabled": {"browser": False}})
    assert r.json()["enabled"]["browser"] is False
    tools = (await client.get("/api/tools")).json()
    assert not next(t for t in tools if t["name"] == "web_search")["enabled"]
    await client.put("/api/permissions", json={"enabled": {"browser": True}})
    r = await client.get("/api/activity")
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_schedules(client):
    r = await client.post("/api/schedules", json={"name": "r", "kind": "reminder", "payload": "stand up", "schedule_type": "interval", "interval_seconds": 3600})
    assert r.status_code == 200
    jid = r.json()["id"]
    assert any(j["id"] == jid for j in (await client.get("/api/schedules")).json())
    r = await client.post("/api/schedules", json={"name": "bad", "kind": "reminder", "payload": "x", "schedule_type": "cron", "cron": "bad"})
    assert r.status_code == 400
    assert (await client.delete(f"/api/schedules/{jid}")).json()["deleted"]


@pytest.mark.asyncio
async def test_voice_disabled_reports_clearly(client):
    r = await client.post("/api/voice/speak", json={"text": "hello"})
    assert r.status_code == 503 and "disabled" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_auth_token(client, svc):
    svc.settings.api_token = "tok123456"
    try:
        assert (await client.get("/api/ping")).status_code == 200  # ping is public
        assert (await client.get("/api/tasks")).status_code == 401
        assert (await client.get("/api/tasks", headers={"Authorization": "Bearer tok123456"})).status_code == 200
    finally:
        svc.settings.api_token = ""


@pytest.mark.asyncio
async def test_setup_status(client):
    r = await client.get("/api/setup/status")
    body = r.json()
    assert "ollama" in body and "current" in body
