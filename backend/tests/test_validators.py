from pathlib import Path

import pytest

from app.core.exceptions import PathNotAllowed, ToolValidationError
from app.security.validators import resolve_path, validate_command, validate_email, validate_filename, validate_phone, validate_url


def test_path_traversal_blocked(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_path(str(root / ".." / "secret.txt"), [root])
    with pytest.raises(PathNotAllowed):
        resolve_path("C:\\Windows\\System32\\cmd.exe", [root])
    with pytest.raises(PathNotAllowed):
        resolve_path(str(tmp_path / "rootx" / "file"), [root])  # prefix trick


def test_path_inside_root_ok(tmp_path: Path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    p = resolve_path(str(root / "sub" / ".." / "sub" / "a.txt"), [root])
    assert p == (root / "sub" / "a.txt").resolve()


def test_friendly_names(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from app.security import validators

    monkeypatch.setitem(validators._KNOWN_FOLDERS, "downloads", tmp_path / "Downloads")
    (tmp_path / "Downloads").mkdir()
    assert resolve_path("Downloads", [tmp_path]) == (tmp_path / "Downloads").resolve()


def test_protected_locations_for_write(tmp_path: Path):
    root = tmp_path
    win = root / "Windows"
    win.mkdir()
    with pytest.raises(PathNotAllowed):
        resolve_path(str(win / "x.dll"), [root], for_write=True)
    # reading is fine
    resolve_path(str(win / "x.dll"), [root])


def test_null_byte_rejected(tmp_path: Path):
    with pytest.raises(ToolValidationError):
        resolve_path("a\x00b", [tmp_path])


def test_must_exist(tmp_path: Path):
    with pytest.raises(ToolValidationError):
        resolve_path(str(tmp_path / "missing.txt"), [tmp_path], must_exist=True)


def test_filename_validation():
    assert validate_filename("Final_Report.pdf") == "Final_Report.pdf"
    for bad in ("..", "a/b", "a\\b", "con.", "x*y", ""):
        with pytest.raises(ToolValidationError):
            validate_filename(bad)


def test_command_validation():
    assert validate_command("  git status ") == "git status"
    with pytest.raises(ToolValidationError):
        validate_command("")
    with pytest.raises(ToolValidationError):
        validate_command("a\x00b")


def test_url_phone_email():
    assert validate_url("example.com/x") == "https://example.com/x"
    with pytest.raises(ToolValidationError):
        validate_url("javascript:alert(1)")
    assert validate_phone("+91 98765-43210") == "919876543210"
    with pytest.raises(ToolValidationError):
        validate_phone("123")
    assert validate_email("a@b.co") == "a@b.co"
    with pytest.raises(ToolValidationError):
        validate_email("not-an-email")
