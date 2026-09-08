"""Argument validators.  Never trust model-generated paths or commands blindly.

`resolve_path` is the single choke-point for filesystem access: it expands
`~` and environment variables, resolves symlinks/`..`, and verifies the result
lies inside one of the configured allowed roots.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Optional

from app.core.exceptions import PathNotAllowed, ToolValidationError

# Locations that are never writable/deletable even when inside an allowed root.
PROTECTED_DIR_NAMES = {"windows", "system32", "program files", "program files (x86)", "programdata", "$recycle.bin", "boot"}
PROTECTED_FILE_NAMES = {"ntuser.dat", "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr"}

_KNOWN_FOLDERS = {
    "desktop": Path.home() / "Desktop",
    "documents": Path.home() / "Documents",
    "downloads": Path.home() / "Downloads",
    "pictures": Path.home() / "Pictures",
    "music": Path.home() / "Music",
    "videos": Path.home() / "Videos",
    "home": Path.home(),
}


def expand_user_path(raw: str) -> Path:
    """Expand ~, %VAR%, $VAR and friendly names ("Downloads")."""
    if raw is None:
        raise ToolValidationError("path is required")
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        raise ToolValidationError("path is empty")
    if "\x00" in s:
        raise ToolValidationError("path contains a null byte")
    key = s.lower().rstrip("/\\")
    if key in _KNOWN_FOLDERS:
        return _KNOWN_FOLDERS[key]
    s = os.path.expandvars(os.path.expanduser(s))
    return Path(s)


def resolve_path(raw: str, allowed_roots: Iterable[Path], *, must_exist: bool = False, for_write: bool = False) -> Path:
    """Resolve and validate a path against the allowed roots."""
    p = expand_user_path(raw)
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        raise ToolValidationError(f"Cannot resolve path: {e}") from e
    roots = [Path(r).resolve() for r in allowed_roots]
    key = str(raw).strip().strip('"').strip("'").lower().rstrip("/\\")
    if key in _KNOWN_FOLDERS and not any(_is_within(resolved, r) for r in roots):
        # Friendly name whose home-based location is outside the allowed roots:
        # fall back to a same-named folder directly under an allowed root.
        for r in roots:
            cand = r / _KNOWN_FOLDERS[key].name
            if cand.is_dir():
                resolved = cand.resolve()
                break
    if not any(_is_within(resolved, r) for r in roots):
        raise PathNotAllowed(f"'{resolved}' is outside allowed roots {[str(r) for r in roots]}",
                             user_message=f"'{resolved}' is outside the folders I'm allowed to access.")
    if for_write:
        lowered = [part.lower() for part in resolved.parts]
        if any(part in PROTECTED_DIR_NAMES for part in lowered[1:]) or resolved.name.lower() in PROTECTED_FILE_NAMES:
            raise PathNotAllowed(f"'{resolved}' is a protected system location", user_message="That is a protected system location.")
    if must_exist and not resolved.exists():
        raise ToolValidationError(f"Path does not exist: {resolved}", user_message=f"I couldn't find '{resolved}'.")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_within_roots(path: Path, allowed_roots: Iterable[Path]) -> bool:
    return any(_is_within(path.resolve(), Path(r).resolve()) for r in allowed_roots)


_SAFE_NAME_RE = re.compile(r'^[^<>:"/\\|?*\x00-\x1f]{1,255}$')


def validate_filename(name: str) -> str:
    name = str(name).strip()
    if not name or name in (".", "..") or not _SAFE_NAME_RE.match(name):
        raise ToolValidationError(f"Invalid file name: {name!r}")
    if name.rstrip(". ") != name:
        raise ToolValidationError("File names cannot end with a dot or space on Windows")
    return name


def validate_int(value, name: str, lo: Optional[int] = None, hi: Optional[int] = None, default: Optional[int] = None) -> int:
    if value is None:
        if default is None:
            raise ToolValidationError(f"{name} is required")
        return default
    try:
        v = int(value)
    except (TypeError, ValueError) as e:
        raise ToolValidationError(f"{name} must be an integer") from e
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


_URL_RE = re.compile(r"^(https?)://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def validate_url(url: str) -> str:
    url = str(url).strip()
    scheme = re.match(r"^([a-z][a-z0-9+.\-]*):", url, re.IGNORECASE)
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise ToolValidationError(f"Unsupported URL scheme: {scheme.group(1)}")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if not _URL_RE.match(url):
        raise ToolValidationError(f"Invalid URL: {url}")
    return url


def validate_command(command: str, max_len: int = 4000) -> str:
    cmd = str(command).strip()
    if not cmd:
        raise ToolValidationError("command is empty")
    if len(cmd) > max_len:
        raise ToolValidationError("command is too long")
    if "\x00" in cmd:
        raise ToolValidationError("command contains a null byte")
    return cmd


def validate_phone(phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", str(phone))
    if digits.startswith("+"):
        digits = digits[1:]
    if not digits.isdigit() or not (7 <= len(digits) <= 15):
        raise ToolValidationError(f"Invalid phone number: {phone}")
    return digits


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(addr: str) -> str:
    addr = str(addr).strip()
    if not _EMAIL_RE.match(addr):
        raise ToolValidationError(f"Invalid email address: {addr}")
    return addr


def clamp_list(items: Optional[List], max_items: int) -> List:
    return list(items or [])[:max_items]
