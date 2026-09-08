"""Filesystem tools: search, open, read, copy, move, rename, create, delete, metadata.

Every path goes through `resolve_path` (allowed-roots check).  Destructive
operations are DANGEROUS and produce a preview (file count / size) for the
confirmation modal.  Deletes go to the Recycle Bin when possible.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.exceptions import ToolValidationError
from app.plugins.loader import Plugin
from app.security.permissions import RiskLevel
from app.security.validators import resolve_path, validate_filename
from app.tools.base import Tool, ToolContext, ToolResult, run_sync
from app.tools.filesystem.index import FileIndex


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _stat_info(p: Path) -> Dict[str, Any]:
    st = p.stat()
    return {
        "path": str(p), "name": p.name, "is_dir": p.is_dir(), "size": st.st_size, "size_human": _fmt_size(st.st_size),
        "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "created": datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds"),
        "accessed": datetime.fromtimestamp(st.st_atime).isoformat(timespec="seconds"),
        "extension": p.suffix.lower(), "readonly": not (st.st_mode & stat.S_IWRITE),
        "hidden": bool(getattr(st, "st_file_attributes", 0) & 2) if sys.platform == "win32" else p.name.startswith("."),
    }


def _parse_when(value: Optional[str]) -> Optional[float]:
    """Accepts ISO dates or friendly words: today, yesterday, 'last week', '3 days ago', 'last month'."""
    if not value:
        return None
    v = value.strip().lower()
    now = datetime.now()
    if v in ("today",):
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if v == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if v in ("this week", "last week", "week"):
        return (now - timedelta(days=7)).timestamp()
    if v in ("this month", "last month", "month"):
        return (now - timedelta(days=31)).timestamp()
    if v in ("this year", "last year", "year"):
        return (now - timedelta(days=365)).timestamp()
    import re

    m = re.match(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}[unit]
        return (now - timedelta(seconds=n * mult)).timestamp()
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise ToolValidationError(f"Unrecognised date '{value}'. Use ISO format (2025-01-31) or words like 'yesterday', '3 days ago'.")


def _count_tree(p: Path, limit: int = 100000) -> Dict[str, int]:
    files = dirs = size = 0
    if p.is_file():
        return {"files": 1, "dirs": 0, "bytes": p.stat().st_size}
    for root, dnames, fnames in os.walk(p):
        dirs += len(dnames)
        for f in fnames:
            files += 1
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
            if files >= limit:
                return {"files": files, "dirs": dirs, "bytes": size, "truncated": 1}
    return {"files": files, "dirs": dirs, "bytes": size}


# ----------------------------------------------------------------- search
class SearchFilesArgs(BaseModel):
    query: str = Field(default="", description="Words that should appear in the file name (e.g. 'resume', 'bus schedule')")
    content: str = Field(default="", description="Words that should appear INSIDE the file (full-text search of documents)")
    extensions: List[str] = Field(default_factory=list, description="Filter by extension e.g. ['pdf','docx']")
    directory: str = Field(default="", description="Restrict to this folder (e.g. 'Documents', 'C:/Projects')")
    modified_after: str = Field(default="", description="ISO date or 'today', 'yesterday', '3 days ago', 'last week'")
    modified_before: str = Field(default="")
    created_after: str = Field(default="")
    created_before: str = Field(default="")
    min_size_mb: float = Field(default=0, ge=0)
    max_size_mb: float = Field(default=0, ge=0)
    sort: str = Field(default="relevance", description="relevance | modified | size | name")
    limit: int = Field(default=20, ge=1, le=200)


class SearchFilesTool(Tool):
    name = "search_files"
    description = ("Search files on this computer by name, extension, folder, modification/creation date, size, and/or "
                   "full-text content. Use 'query' for file-name words and 'content' for words inside documents. "
                   "Returns paths with sizes and dates. Use sort='size' with no query to find the largest files.")
    category = "filesystem"
    risk_level = RiskLevel.READ_ONLY
    args_model = SearchFilesArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        index: FileIndex = ctx.service("file_index")
        ctx.activity("Searching files…")
        await index.ensure_scanned(timeout=20)
        directory = None
        if args.get("directory"):
            directory = str(resolve_path(args["directory"], ctx.settings.allowed_roots))
        kw = dict(
            query=args.get("query", ""), content=args.get("content", ""), extensions=args.get("extensions") or None,
            directory=directory, modified_after=_parse_when(args.get("modified_after")),
            modified_before=_parse_when(args.get("modified_before")), created_after=_parse_when(args.get("created_after")),
            created_before=_parse_when(args.get("created_before")),
            min_size=int(args["min_size_mb"] * 1024 * 1024) if args.get("min_size_mb") else None,
            max_size=int(args["max_size_mb"] * 1024 * 1024) if args.get("max_size_mb") else None,
            sort=args.get("sort", "relevance"), limit=args.get("limit", 20),
        )
        results = await index.search(**kw)
        for r in results:
            r["size_human"] = _fmt_size(r["size"])
        if not results and kw["query"] and not index.status()["running"]:
            # Fall back to a live (slow) walk of allowed roots limited in time
            results = await run_sync(_live_search, ctx.settings.allowed_roots, kw["query"], kw["limit"], ctx.settings.index_excludes)
        ctx.activity(f"Found {len(results)} matching files")
        status = index.status()
        note = "Index still building; results may be incomplete." if status["running"] else ""
        return ToolResult.ok({"results": results, "count": len(results), "note": note},
                             summary=f"Found {len(results)} files", untrusted=bool(kw["content"]), source="file")


def _live_search(roots: List[Path], query: str, limit: int, excludes: List[str], time_budget: float = 15.0) -> List[Dict[str, Any]]:
    import time

    tokens = query.lower().split()
    out: List[Dict[str, Any]] = []
    start = time.monotonic()
    for root in roots:
        for r, dnames, fnames in os.walk(root):
            dnames[:] = [d for d in dnames if d.lower() not in excludes]
            for f in fnames:
                if all(t in f.lower() for t in tokens):
                    p = Path(r) / f
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    out.append({"path": str(p), "name": f, "ext": p.suffix.lower(), "size": st.st_size, "size_human": _fmt_size(st.st_size),
                                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                                "created": datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds"), "is_dir": False})
                    if len(out) >= limit:
                        return out
            if time.monotonic() - start > time_budget:
                return out
    return out


class SemanticSearchArgs(BaseModel):
    query: str = Field(description="Natural-language description of the document's topic", min_length=2)
    limit: int = Field(default=10, ge=1, le=50)


class SemanticSearchTool(Tool):
    name = "semantic_search_files"
    description = ("Find documents by meaning/topic (e.g. 'the document where I discussed multiline bus scheduling'). "
                   "Uses embeddings when configured, otherwise full-text keyword search over document contents.")
    category = "filesystem"
    risk_level = RiskLevel.READ_ONLY
    args_model = SemanticSearchArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        index: FileIndex = ctx.service("file_index")
        await index.ensure_scanned(timeout=20)
        results, mode = await index.semantic_search(args["query"], args.get("limit", 10))
        return ToolResult.ok({"results": results, "mode": mode, "count": len(results)},
                             summary=f"{len(results)} results ({mode})", untrusted=True, source="file")


class RebuildIndexArgs(BaseModel):
    pass


class RebuildIndexTool(Tool):
    name = "rebuild_file_index"
    description = "Rescan the configured folders to refresh the file index (incremental; safe to run)."
    category = "filesystem"
    risk_level = RiskLevel.SAFE
    args_model = RebuildIndexArgs
    timeout_seconds = 1800

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        index: FileIndex = ctx.service("file_index")
        ctx.activity("Rebuilding file index…")
        stats = await index.scan()
        return ToolResult.ok(index.status(), summary=f"Indexed {stats.files} files in {stats.last_run_seconds}s")


# ------------------------------------------------------------- open/read
class PathArgs(BaseModel):
    path: str = Field(description="Absolute path, a friendly folder name like 'Downloads', or just a file/folder name to look up")


_SEP_RE = re.compile(r"[\s_\-.,()\[\]+]+")


def _tokens(text: str) -> List[str]:
    return [t for t in _SEP_RE.split(text.lower()) if t]


def _token_hit(token: str, name_tokens: List[str]) -> float:
    """1.0 exact, 0.9 prefix, else best difflib ratio if >= 0.8 (so 'parts' ~ 'paths', 'resume' ~ 'résumé')."""
    import difflib

    best = 0.0
    for nt in name_tokens:
        if nt == token:
            return 1.0
        if nt.startswith(token) or token.startswith(nt) and len(nt) >= 3:
            best = max(best, 0.9)
            continue
        r = difflib.SequenceMatcher(None, token, nt).ratio()
        if r >= 0.8:
            best = max(best, r)
    return best


def _match_score(query: str, name: str) -> float:
    """0..1 how well a file/folder name matches the words in `query` (separator- and typo-tolerant)."""
    q = _tokens(query)
    if not q:
        return 0.0
    stem = name.rsplit(".", 1)[0] if "." in name else name
    n = _tokens(stem) + ([name.rsplit(".", 1)[1].lower()] if "." in name else [])
    if not n:
        return 0.0
    hits = [_token_hit(t, n) for t in q]
    coverage = sum(hits) / len(q)
    if " ".join(_tokens(stem)) == " ".join(q):
        return 1.0
    return coverage


def _live_find_any(roots: List[Path], query: str, limit: int, excludes: List[str], time_budget: float = 12.0,
                   min_score: float = 0.75) -> List[Dict[str, Any]]:
    """Walk the roots (time-boxed) collecting files AND folders whose name fuzzily matches `query`."""
    import time

    out: List[Dict[str, Any]] = []
    start = time.monotonic()
    for root in roots:
        for r, dnames, fnames in os.walk(root):
            dnames[:] = [d for d in dnames if d.lower() not in excludes]
            for name, is_dir in [(d, True) for d in dnames] + [(f, False) for f in fnames]:
                sc = _match_score(query, name)
                if sc >= min_score:
                    out.append({"path": str(Path(r) / name), "name": name, "is_dir": is_dir, "score": sc})
                    if len(out) >= limit:
                        return out
            if time.monotonic() - start > time_budget:
                return out
    return out


def _rank(query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for c in candidates:
        c["score"] = c.get("score") or _match_score(query, c["name"])
    return sorted(candidates, key=lambda c: (round(c["score"], 3), -len(c["name"])), reverse=True)


async def locate(raw: str, ctx: ToolContext, *, want_dir: Optional[bool] = None, limit: int = 8) -> List[Dict[str, Any]]:
    """Find files/folders whose name matches `raw` (a bare name, not a path) inside the allowed roots.

    Tolerant of spaces vs underscores and small mis-hearings ("certification parts" finds
    "AWS_certification_paths.pdf").  Uses the file index first, then a time-boxed live walk.
    """
    index: FileIndex = ctx.service("file_index")
    name = Path(raw.strip().strip('"')).name
    toks = _tokens(name)
    if not toks:
        return []
    results: List[Dict[str, Any]] = []
    try:
        await index.ensure_scanned(timeout=10)
        seen = set()
        for q in ([name] + [t for t in toks if len(t) >= 3][:2]):
            for r in await index.search(query=q, include_dirs=True, limit=200):
                if r["path"] not in seen:
                    seen.add(r["path"])
                    results.append(r)
    except Exception:  # noqa: BLE001
        results = []
    results = [r for r in results if _match_score(name, r["name"]) >= 0.75]
    if want_dir is not None:
        results = [r for r in results if bool(r.get("is_dir")) == want_dir]
    if not results:
        live = await run_sync(_live_find_any, list(ctx.settings.allowed_roots), name, 60, ctx.settings.index_excludes)
        if want_dir is not None:
            live = [r for r in live if r["is_dir"] == want_dir]
        results = live
    return _rank(name, results)[:limit]


def _looks_like_path(raw: str) -> bool:
    r = raw.strip().strip('"')
    return ("\\" in r) or ("/" in r) or (len(r) > 1 and r[1] == ":")


async def resolve_or_locate(raw: str, ctx: ToolContext, *, want_dir: Optional[bool] = None):
    """Resolve an explicit path; if it does not exist (or is just a name), look it up by name.

    Returns (path, None) on success or (None, ToolResult) describing what was found / not found.
    """
    from app.core.exceptions import PathNotAllowed

    try:
        p = resolve_path(raw, ctx.settings.allowed_roots, must_exist=True)
        return p, None
    except PathNotAllowed:
        if _looks_like_path(raw):
            raise  # an explicit path outside the allowed roots: report it honestly
        # A bare name ("Games", "report.pdf") resolved relative to the working directory; look it up instead.
    except ToolValidationError:
        pass
    ctx.activity(f"Looking for '{Path(raw).name}'…")
    cands = await locate(raw, ctx, want_dir=want_dir)
    if not cands:
        kind = "folder" if want_dir else "file or folder"
        return None, ToolResult.fail(f"I couldn't find a {kind} named '{raw}' in the folders I can access ({'; '.join(str(r) for r in ctx.settings.allowed_roots)}). "
                                     "Try search_files with different words, or ask the user where it is.")
    best = cands[0]
    top = best["score"]
    runner = cands[1]["score"] if len(cands) > 1 else 0.0
    if len(cands) == 1 or top >= 0.999 and runner < 0.999 or (top - runner) >= 0.15:
        return Path(best["path"]), None
    listing = "\n".join(f"- {c['path']}" for c in cands)
    return None, ToolResult.fail(f"Several matches for '{raw}'. Ask the user which one, then call again with the full path:\n{listing}")


class OpenFileTool(Tool):
    name = "open_file"
    description = ("Open a file or folder with its default Windows application (like double-clicking it). "
                   "Accepts a full path or just the file name; names are looked up in the allowed folders.")
    category = "filesystem"
    risk_level = RiskLevel.SAFE
    args_model = PathArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p, err = await resolve_or_locate(args["path"], ctx)
        if err:
            return err
        if p.suffix.lower() in (".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js", ".scr", ".com"):
            return ToolResult.fail(f"Refusing to open executable '{p.name}' via open_file. Use launch_application or the terminal with confirmation.")
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except AttributeError:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(p)])
        except OSError as e:
            return ToolResult.fail(f"Windows could not open '{p}': {e.strerror or e}")
        return ToolResult.ok({"opened": str(p)}, summary=f"Opened {p.name}")

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Open {args.get('path')}"


class OpenFolderTool(Tool):
    name = "open_folder"
    description = ("Open a folder in Windows Explorer (full path or folder name, looked up if needed). "
                   "If a file path is given, Explorer opens with that file selected. To open a FILE, use open_file instead.")
    category = "filesystem"
    risk_level = RiskLevel.SAFE
    args_model = PathArgs
    available_in_cloud = False

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p, err = await resolve_or_locate(args["path"], ctx, want_dir=True)
        if err:
            return err
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["explorer", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
        return ToolResult.ok({"opened": str(p)}, summary=f"Opened folder {p.name or p}")


class ReadFileArgs(BaseModel):
    path: str
    max_chars: int = Field(default=20000, ge=100, le=200000)
    offset: int = Field(default=0, ge=0, description="Character offset to start from (for long files)")


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read the text content of a file (txt, md, code, csv, json, pdf, docx, xlsx). Returns up to max_chars."
    category = "filesystem"
    risk_level = RiskLevel.READ_ONLY
    args_model = ReadFileArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, must_exist=True)
        if p.is_dir():
            return ToolResult.fail(f"'{p}' is a folder. Use list_directory instead.")
        if p.stat().st_size > ctx.settings.fs_max_read_bytes:
            return ToolResult.fail(f"File is too large to read ({_fmt_size(p.stat().st_size)}).")
        index: FileIndex = ctx.service("file_index")
        text = await run_sync(index.extract_text, str(p), p.suffix.lower())
        if text is None:
            try:
                raw = await run_sync(p.read_bytes)
                text = raw.decode("utf-8", errors="ignore")
                if "\x00" in text[:4000]:
                    return ToolResult.fail(f"'{p.name}' looks like a binary file; I can't read it as text.")
            except OSError as e:
                return ToolResult.fail(f"Could not read '{p}': {e.strerror or e}")
        off = args.get("offset", 0)
        chunk = text[off: off + args.get("max_chars", 20000)]
        return ToolResult.ok({"path": str(p), "total_chars": len(text), "offset": off, "content": chunk,
                              "truncated": off + len(chunk) < len(text)},
                             summary=f"Read {len(chunk)} chars from {p.name}", untrusted=True, source="file")


class ListDirArgs(BaseModel):
    path: str = Field(default="~", description="Folder to list")
    limit: int = Field(default=200, ge=1, le=2000)
    include_hidden: bool = False


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List the files and folders inside a directory with sizes and dates."
    category = "filesystem"
    risk_level = RiskLevel.READ_ONLY
    args_model = ListDirArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, must_exist=True)
        if not p.is_dir():
            return ToolResult.fail(f"'{p}' is not a folder")

        def _list():
            items = []
            try:
                with os.scandir(p) as it:
                    for e in it:
                        if not args.get("include_hidden") and e.name.startswith("."):
                            continue
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        items.append({"name": e.name, "path": e.path, "is_dir": e.is_dir(follow_symlinks=False), "size": st.st_size,
                                      "size_human": _fmt_size(st.st_size),
                                      "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
            except PermissionError:
                raise
            items.sort(key=lambda i: (not i["is_dir"], i["name"].lower()))
            return items

        try:
            items = await run_sync(_list)
        except PermissionError:
            return ToolResult.fail(f"Windows denied access to '{p}'.")
        total = len(items)
        return ToolResult.ok({"path": str(p), "count": total, "items": items[: args.get("limit", 200)]},
                             summary=f"{total} items in {p.name or p}")


class FileInfoTool(Tool):
    name = "file_info"
    description = "Get metadata for a file or folder: size, dates, type, attributes; for folders also file counts."
    category = "filesystem"
    risk_level = RiskLevel.READ_ONLY
    args_model = PathArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, must_exist=True)
        info = await run_sync(_stat_info, p)
        if p.is_dir():
            counts = await run_sync(_count_tree, p, 50000)
            info.update({"contains_files": counts["files"], "contains_dirs": counts["dirs"], "total_size": counts["bytes"],
                         "total_size_human": _fmt_size(counts["bytes"])})
        return ToolResult.ok(info, summary=f"Info for {p.name}")


# ---------------------------------------------------------------- writes
class CreateFolderArgs(BaseModel):
    path: str = Field(description="Folder path to create (parents are created as needed)")


class CreateFolderTool(Tool):
    name = "create_folder"
    description = "Create a new folder (and any missing parent folders)."
    category = "filesystem"
    risk_level = RiskLevel.SENSITIVE
    args_model = CreateFolderArgs

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, for_write=True)
        if p.exists():
            return ToolResult.ok({"path": str(p), "existed": True}, summary="Folder already exists")
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ToolResult.fail(f"Could not create '{p}': {e.strerror or e}")
        return ToolResult.ok({"path": str(p), "existed": False}, summary=f"Created folder {p.name}")


class WriteFileArgs(BaseModel):
    path: str
    content: str = Field(description="Text content to write")
    append: bool = Field(default=False)
    overwrite: bool = Field(default=False, description="Must be true to replace an existing file")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a text file with the given content. Overwriting an existing file requires overwrite=true."
    category = "filesystem"
    risk_level = RiskLevel.SENSITIVE
    args_model = WriteFileArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        return RiskLevel.DANGEROUS if args.get("overwrite") else RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        verb = "Append to" if args.get("append") else ("Overwrite" if args.get("overwrite") else "Create")
        return f"{verb} file {args.get('path')} ({len(args.get('content', ''))} chars)"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = resolve_path(args["path"], ctx.settings.allowed_roots, for_write=True)
        if p.exists() and p.is_dir():
            return ToolResult.fail(f"'{p}' is a folder")
        if p.exists() and not args.get("append") and not args.get("overwrite"):
            return ToolResult.fail(f"'{p}' already exists. Set overwrite=true to replace it (this needs confirmation).")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if args.get("append") else "w"
            with open(p, mode, encoding="utf-8") as f:
                f.write(args["content"])
        except OSError as e:
            return ToolResult.fail(f"Could not write '{p}': {e.strerror or e}")
        return ToolResult.ok({"path": str(p), "bytes": p.stat().st_size}, summary=f"Wrote {p.name}")


class CopyMoveArgs(BaseModel):
    source: str
    destination: str = Field(description="Destination file path or existing folder")
    overwrite: bool = False


class CopyTool(Tool):
    name = "copy_file"
    description = "Copy a file or folder to a destination path or folder."
    category = "filesystem"
    risk_level = RiskLevel.SENSITIVE
    args_model = CopyMoveArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        return RiskLevel.DANGEROUS if args.get("overwrite") else RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Copy {args.get('source')} -> {args.get('destination')}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        roots = ctx.settings.allowed_roots
        src = resolve_path(args["source"], roots, must_exist=True)
        dst = resolve_path(args["destination"], roots, for_write=True)
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() and not args.get("overwrite"):
            return ToolResult.fail(f"'{dst}' already exists. Set overwrite=true to replace it.")
        if src.is_dir() and dst.is_relative_to(src):
            return ToolResult.fail("Cannot copy a folder into itself.")
        try:
            if src.is_dir():
                await run_sync(shutil.copytree, src, dst, dirs_exist_ok=bool(args.get("overwrite")))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                await run_sync(shutil.copy2, src, dst)
        except OSError as e:
            return ToolResult.fail(f"Copy failed: {e.strerror or e}")
        return ToolResult.ok({"source": str(src), "destination": str(dst)}, summary=f"Copied to {dst.name}")


class MoveTool(Tool):
    name = "move_file"
    description = "Move a file or folder to a destination path or folder."
    category = "filesystem"
    risk_level = RiskLevel.SENSITIVE
    args_model = CopyMoveArgs

    def classify(self, args: Dict[str, Any]) -> RiskLevel:
        return RiskLevel.DANGEROUS if args.get("overwrite") else RiskLevel.SENSITIVE

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Move {args.get('source')} -> {args.get('destination')}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        roots = ctx.settings.allowed_roots
        src = resolve_path(args["source"], roots, must_exist=True, for_write=True)
        dst = resolve_path(args["destination"], roots, for_write=True)
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() and not args.get("overwrite"):
            return ToolResult.fail(f"'{dst}' already exists. Set overwrite=true to replace it.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and args.get("overwrite"):
                if dst.is_dir():
                    await run_sync(shutil.rmtree, dst)
                else:
                    dst.unlink()
            await run_sync(shutil.move, str(src), str(dst))
        except OSError as e:
            return ToolResult.fail(f"Move failed: {e.strerror or e}")
        return ToolResult.ok({"source": str(src), "destination": str(dst)}, summary=f"Moved to {dst.name}")


class RenameArgs(BaseModel):
    path: str
    new_name: str = Field(description="New file/folder name (not a path)")


class RenameTool(Tool):
    name = "rename_file"
    description = "Rename a file or folder in place."
    category = "filesystem"
    risk_level = RiskLevel.SENSITIVE
    args_model = RenameArgs

    def describe(self, args: Dict[str, Any]) -> str:
        return f"Rename {args.get('path')} -> {args.get('new_name')}"

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        src = resolve_path(args["path"], ctx.settings.allowed_roots, must_exist=True, for_write=True)
        new_name = validate_filename(args["new_name"])
        dst = src.with_name(new_name)
        if dst.exists():
            return ToolResult.fail(f"'{dst.name}' already exists in that folder.")
        try:
            src.rename(dst)
        except OSError as e:
            return ToolResult.fail(f"Rename failed: {e.strerror or e}")
        return ToolResult.ok({"old": str(src), "new": str(dst)}, summary=f"Renamed to {new_name}")


class DeleteArgs(BaseModel):
    paths: List[str] = Field(description="Files/folders to delete", min_length=1, max_length=500)
    permanent: bool = Field(default=False, description="Skip the Recycle Bin (irreversible)")


class DeleteTool(Tool):
    name = "delete_files"
    description = ("Delete files or folders. Items go to the Recycle Bin unless permanent=true. "
                   "This always asks the user for confirmation.")
    category = "filesystem"
    risk_level = RiskLevel.DANGEROUS
    requires_confirmation = True
    args_model = DeleteArgs

    def describe(self, args: Dict[str, Any]) -> str:
        n = len(args.get("paths", []))
        kind = "permanently delete" if args.get("permanent") else "move to Recycle Bin"
        return f"{kind.capitalize()} {n} item(s): " + ", ".join(args.get("paths", [])[:3]) + (" …" if n > 3 else "")

    async def preview(self, args: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        files = dirs = size = 0
        resolved = []
        for raw in args["paths"]:
            try:
                p = resolve_path(raw, ctx.settings.allowed_roots, must_exist=True, for_write=True)
            except Exception as e:  # noqa: BLE001
                resolved.append({"path": raw, "error": str(e)})
                continue
            c = await run_sync(_count_tree, p, 20000)
            files += c["files"]; dirs += c["dirs"]; size += c["bytes"]
            resolved.append({"path": str(p), "is_dir": p.is_dir(), "files": c["files"], "size_human": _fmt_size(c["bytes"])})
        kind = "permanently delete" if args.get("permanent") else "move to the Recycle Bin"
        return {"items": resolved, "total_files": files, "total_dirs": dirs, "total_size": _fmt_size(size),
                "description": f"This will {kind} {files} file(s) and {dirs} folder(s) ({_fmt_size(size)})."}

    async def run(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        deleted, errors = [], []
        for raw in args["paths"]:
            try:
                p = resolve_path(raw, ctx.settings.allowed_roots, must_exist=True, for_write=True)
            except Exception as e:  # noqa: BLE001
                errors.append({"path": raw, "error": getattr(e, "user_message", str(e))})
                continue
            try:
                if args.get("permanent") or not await run_sync(_recycle, p):
                    if p.is_dir():
                        await run_sync(shutil.rmtree, p)
                    else:
                        p.unlink()
                deleted.append(str(p))
            except OSError as e:
                errors.append({"path": str(p), "error": e.strerror or str(e)})
        summary = f"Deleted {len(deleted)} item(s)" + (f", {len(errors)} failed" if errors else "")
        return ToolResult(success=not errors or bool(deleted), output={"deleted": deleted, "errors": errors},
                          error=None if not errors else summary, summary=summary)


def _recycle(p: Path) -> bool:
    """Send to Recycle Bin via the Windows shell. Returns False if unavailable."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = str(p) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0 and not op.fAnyOperationsAborted and not p.exists()
    except Exception:  # noqa: BLE001
        return False


def _build(settings) -> List[Tool]:
    return [SearchFilesTool(), SemanticSearchTool(), RebuildIndexTool(), OpenFileTool(), OpenFolderTool(), ReadFileTool(),
            ListDirectoryTool(), FileInfoTool(), CreateFolderTool(), WriteFileTool(), CopyTool(), MoveTool(), RenameTool(), DeleteTool()]


PLUGIN = Plugin(
    name="filesystem",
    description="Search, open, read and manage files within allowed folders",
    permissions=["filesystem"],
    tools=_build,
    configuration={"FS_ALLOWED_ROOTS": "semicolon-separated folders Zeta may access", "FS_INDEX_ROOTS": "folders to index",
                   "FS_INDEX_CONTENT": "index document text for content search"},
)
