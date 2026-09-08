import pytest


@pytest.mark.asyncio
async def test_add_search_update_delete(svc):
    ltm = svc.long_term
    a = await ltm.add("User prefers concise responses", "preference")
    b = await ltm.add("Main project repository is D:/Projects/Zeta", "project", ["zeta", "repo"])
    res = await ltm.search("where is the project repository")
    assert res and res[0]["id"] == b.id
    upd = await ltm.update(a.id, content="User prefers very concise responses", importance=0.9)
    assert upd.importance == 0.9
    assert await ltm.delete(a.id)
    assert not await ltm.delete(a.id)


@pytest.mark.asyncio
async def test_forget_by_topic(svc):
    ltm = svc.long_term
    await ltm.add("Rahul's phone number is +91 98765 43210", "contact", ["rahul"])
    await ltm.add("The UBA project deadline is Friday", "project")
    n = await ltm.forget_matching("Rahul")
    assert n == 1
    assert len(await ltm.list()) == 1


@pytest.mark.asyncio
async def test_context_block_includes_preferences(svc):
    ltm = svc.long_term
    await ltm.add("User prefers concise responses", "preference")
    await ltm.add("The office printer is on floor 3", "fact")
    block = await ltm.context_block("what is the weather")
    assert "concise" in block


@pytest.mark.asyncio
async def test_dedup(svc):
    ltm = svc.long_term
    await ltm.add("same fact")
    await ltm.add("same fact")
    assert len(await ltm.list()) == 1


@pytest.mark.asyncio
async def test_short_term_history(svc):
    stm = svc.short_term
    cid = await stm.ensure_conversation(None, "hello")
    await stm.append(cid, "user", "hello")
    await stm.append(cid, "assistant", "hi")
    hist = await stm.history(cid)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    convs = await stm.list_conversations()
    assert convs[0].title == "hello"
