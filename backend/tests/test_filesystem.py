import pytest

from app.tools.base import ToolContext


def ctx_for(svc, task_id="t1"):
    return ToolContext(settings=svc.settings, secrets=svc.secrets, permissions=svc.permissions, task_id=task_id,
                       services=svc.tool_services())


@pytest.mark.asyncio
async def test_index_and_search(svc, sandbox):
    await svc.file_index.scan()
    status = svc.file_index.status()
    assert status["files"] >= 4
    tool = svc.registry.get("search_files")
    r = await tool.run(tool.validate({"query": "bus schedule"}), ctx_for(svc))
    assert r.success and r.output["count"] == 1
    assert r.output["results"][0]["name"] == "bus_schedule_notes.txt"


@pytest.mark.asyncio
async def test_search_filters(svc, sandbox):
    await svc.file_index.scan()
    tool = svc.registry.get("search_files")
    r = await tool.run(tool.validate({"extensions": ["pdf"]}), ctx_for(svc))
    assert [x["name"] for x in r.output["results"]] == ["report.pdf"]
    r = await tool.run(tool.validate({"sort": "size", "limit": 1}), ctx_for(svc))
    assert r.output["results"][0]["name"] == "big.bin"
    r = await tool.run(tool.validate({"modified_after": "yesterday", "directory": "Documents"}), ctx_for(svc))
    assert r.output["count"] == 2
    r = await tool.run(tool.validate({"min_size_mb": 0.1}), ctx_for(svc))
    assert r.output["results"][0]["name"] == "big.bin"


@pytest.mark.asyncio
async def test_content_search(svc, sandbox):
    await svc.file_index.scan()
    tool = svc.registry.get("search_files")
    r = await tool.run(tool.validate({"content": "multiline bus scheduling"}), ctx_for(svc))
    assert r.success and r.untrusted
    assert r.output["results"][0]["name"] == "bus_schedule_notes.txt"
    assert "snippet" in r.output["results"][0]


@pytest.mark.asyncio
async def test_semantic_search_falls_back(svc, sandbox):
    await svc.file_index.scan()
    tool = svc.registry.get("semantic_search_files")
    r = await tool.run(tool.validate({"query": "bus scheduling documentation"}), ctx_for(svc))
    assert r.success and r.output["count"] >= 1
    assert "fulltext" in r.output["mode"]


@pytest.mark.asyncio
async def test_incremental_rescan_detects_changes(svc, sandbox):
    await svc.file_index.scan()
    (sandbox / "Documents" / "new_file.txt").write_text("hello", encoding="utf-8")
    (sandbox / "Downloads" / "report.pdf").unlink()
    await svc.file_index.scan()
    res = await svc.file_index.search(query="new_file")
    assert len(res) == 1
    res = await svc.file_index.search(query="report")
    assert res == []


@pytest.mark.asyncio
async def test_read_write_copy_move_rename_delete(svc, sandbox):
    c = ctx_for(svc)
    reg = svc.registry
    r = await reg.get("read_file").run({"path": str(sandbox / "Documents" / "resume_2025.md"), "max_chars": 20000, "offset": 0}, c)
    assert r.success and "Senior engineer" in r.output["content"] and r.untrusted

    r = await reg.get("write_file").run(reg.get("write_file").validate({"path": str(sandbox / "Documents" / "notes.txt"), "content": "abc"}), c)
    assert r.success and (sandbox / "Documents" / "notes.txt").read_text() == "abc"
    r = await reg.get("write_file").run(reg.get("write_file").validate({"path": str(sandbox / "Documents" / "notes.txt"), "content": "x"}), c)
    assert not r.success  # exists, overwrite not set

    r = await reg.get("copy_file").run(reg.get("copy_file").validate({"source": str(sandbox / "Documents" / "notes.txt"), "destination": str(sandbox / "Downloads")}), c)
    assert r.success and (sandbox / "Downloads" / "notes.txt").exists()

    r = await reg.get("rename_file").run(reg.get("rename_file").validate({"path": str(sandbox / "Downloads" / "notes.txt"), "new_name": "Final_Report.txt"}), c)
    assert r.success and (sandbox / "Downloads" / "Final_Report.txt").exists()

    r = await reg.get("move_file").run(reg.get("move_file").validate({"source": str(sandbox / "Downloads" / "Final_Report.txt"), "destination": str(sandbox / "Documents")}), c)
    assert r.success and (sandbox / "Documents" / "Final_Report.txt").exists()

    r = await reg.get("create_folder").run({"path": str(sandbox / "Documents" / "New" / "Deep")}, c)
    assert r.success and (sandbox / "Documents" / "New" / "Deep").is_dir()

    tool = reg.get("delete_files")
    args = tool.validate({"paths": [str(sandbox / "Documents" / "Final_Report.txt")], "permanent": True})
    preview = await tool.preview(args, c)
    assert preview["total_files"] == 1
    r = await tool.run(args, c)
    assert r.success and not (sandbox / "Documents" / "Final_Report.txt").exists()


@pytest.mark.asyncio
async def test_outside_root_rejected(svc, sandbox, tmp_path):
    from app.core.exceptions import PathNotAllowed

    c = ctx_for(svc)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(PathNotAllowed):
        await svc.registry.get("read_file").run({"path": str(outside), "max_chars": 100, "offset": 0}, c)


@pytest.mark.asyncio
async def test_list_directory_and_info(svc, sandbox):
    c = ctx_for(svc)
    r = await svc.registry.get("list_directory").run({"path": str(sandbox / "Documents"), "limit": 200, "include_hidden": False}, c)
    assert r.success and r.output["count"] == 2
    r = await svc.registry.get("file_info").run({"path": str(sandbox / "Downloads" / "big.bin")}, c)
    assert r.success and r.output["size"] == 200_000


def test_tool_schemas_are_valid(svc):
    for t in svc.registry.all():
        s = t.schema()
        assert s["function"]["name"] == t.name
        assert s["function"]["parameters"]["type"] == "object"
        assert t.description
