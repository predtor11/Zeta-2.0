"""Filesystem index.

A persistent SQLite index (separate file from the main DB) of file metadata
plus an FTS5 full-text index of extracted text for supported document types.

* Incremental: directories whose mtime is unchanged are not re-listed.
* Content extraction: txt/md/code, PDF (pypdf), DOCX (python-docx), XLSX (openpyxl)
  when those optional packages are installed.
* Semantic search: optional; when an embedding provider is configured, the first
  ~2000 characters of each indexed document are embedded and can be searched
  by cosine similarity.  Without an embedder, `semantic_search` falls back to
  full-text search and says so.

All blocking work runs in a thread so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".php", ".html", ".htm", ".css", ".scss", ".sql", ".sh", ".ps1", ".bat", ".cmd", ".xml", ".tex", ".bib", ".env",
    ".gitignore", ".dockerfile", ".r", ".m", ".kt", ".swift", ".dart", ".lua", ".pl", ".vue", ".svelte",
}
DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}
MAX_CONTENT_CHARS = 300_000
EMBED_CHARS = 2000


@dataclass
class IndexStats:
    files: int = 0
    dirs: int = 0
    content_indexed: int = 0
    last_run_seconds: float = 0.0
    last_run_at: float = 0.0
    running: bool = False
    roots: List[str] = None  # type: ignore


class FileIndex:
    def __init__(self, db_path: Path, roots: List[Path], excludes: List[str], *, index_content: bool = True,
                 content_max_bytes: int = 2_000_000, embedder=None, embedding_model: str = ""):
        self.db_path = Path(db_path)
        self.roots = [Path(r) for r in roots]
        self.excludes = {e.lower() for e in excludes}
        self.index_content = index_content
        self.content_max_bytes = content_max_bytes
        self.embedder = embedder
        self.embedding_model = embedding_model or None
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.stats = IndexStats(roots=[str(r) for r in self.roots])
        self._task: Optional[asyncio.Task] = None
        self._fts_available = True

    # ------------------------------------------------------------------ db
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        c = self._conn
        assert c is not None
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, name TEXT, name_lower TEXT, ext TEXT, parent TEXT,
                size INTEGER, mtime REAL, ctime REAL, is_dir INTEGER, run INTEGER,
                content_mtime REAL DEFAULT 0, embedding TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_files_name ON files(name_lower);
            CREATE INDEX IF NOT EXISTS ix_files_ext ON files(ext);
            CREATE INDEX IF NOT EXISTS ix_files_parent ON files(parent);
            CREATE INDEX IF NOT EXISTS ix_files_mtime ON files(mtime);
            CREATE INDEX IF NOT EXISTS ix_files_size ON files(size);
            CREATE INDEX IF NOT EXISTS ix_files_run ON files(run);
            CREATE TABLE IF NOT EXISTS dirs (path TEXT PRIMARY KEY, mtime REAL, run INTEGER);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(path UNINDEXED, body, tokenize='porter unicode61')")
        except sqlite3.OperationalError as e:
            log.warning("FTS5 unavailable (%s); content search disabled", e)
            self._fts_available = False
        c.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ scanning
    def _excluded(self, name: str, path: str) -> bool:
        """Exclude directories by name (node_modules, .git, AppData...). Ancestors of a root are never considered."""
        return name.lower() in self.excludes

    def scan_sync(self, roots: Optional[Iterable[Path]] = None, progress=None) -> IndexStats:
        started = time.monotonic()
        roots = [Path(r) for r in (roots or self.roots)]
        with self._lock:
            db = self._db()
            run = int(db.execute("SELECT COALESCE(MAX(run),0)+1 FROM files").fetchone()[0])
            self.stats.running = True
            dir_mtimes = dict(db.execute("SELECT path, mtime FROM dirs").fetchall())
            n_files = n_dirs = 0
            batch: List[Tuple] = []
            unchanged_dirs: List[str] = []
            for root in roots:
                if not root.exists():
                    continue
                stack = [str(root)]
                while stack:
                    d = stack.pop()
                    try:
                        st = os.stat(d)
                    except OSError:
                        continue
                    n_dirs += 1
                    unchanged = dir_mtimes.get(d) == st.st_mtime
                    try:
                        with os.scandir(d) as it:
                            entries = list(it)
                    except (PermissionError, OSError):
                        continue
                    for e in entries:
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if self._excluded(e.name, e.path):
                                    continue
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False) and not unchanged:
                                s = e.stat(follow_symlinks=False)
                                ext = os.path.splitext(e.name)[1].lower()
                                batch.append((e.path, e.name, e.name.lower(), ext, d, s.st_size, s.st_mtime, s.st_ctime, 0, run))
                                n_files += 1
                        except OSError:
                            continue
                    if unchanged:
                        unchanged_dirs.append(d)
                    else:
                        batch.append((d, os.path.basename(d) or d, (os.path.basename(d) or d).lower(), "", os.path.dirname(d),
                                      0, st.st_mtime, st.st_ctime, 1, run))
                    db.execute("INSERT OR REPLACE INTO dirs(path, mtime, run) VALUES (?,?,?)", (d, st.st_mtime, run))
                    if len(batch) >= 2000:
                        self._flush(db, batch)
                        batch.clear()
                        if progress:
                            progress(n_files)
            self._flush(db, batch)
            # entries under unchanged directories keep their previous rows; bump their run so they survive the sweep
            for d in unchanged_dirs:
                db.execute("UPDATE files SET run=? WHERE parent=? OR path=?", (run, d, d))
            # sweep: rows not seen in this run under scanned roots are gone
            for root in roots:
                like = str(root).rstrip("\\/") + os.sep + "%"
                stale = [r[0] for r in db.execute("SELECT path FROM files WHERE run<>? AND (path LIKE ? OR path=?)", (run, like, str(root))).fetchall()]
                if stale:
                    db.executemany("DELETE FROM files WHERE path=?", [(p,) for p in stale])
                    if self._fts_available:
                        db.executemany("DELETE FROM content WHERE path=?", [(p,) for p in stale])
                    db.executemany("DELETE FROM dirs WHERE path=?", [(p,) for p in stale])
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_run', ?)", (str(time.time()),))
            db.commit()
            self.stats.files = int(db.execute("SELECT COUNT(*) FROM files WHERE is_dir=0").fetchone()[0])
            self.stats.dirs = int(db.execute("SELECT COUNT(*) FROM files WHERE is_dir=1").fetchone()[0])
            self.stats.last_run_seconds = round(time.monotonic() - started, 2)
            self.stats.last_run_at = time.time()
            self.stats.running = False
        log.info("File index scan complete: %d files, %d dirs in %.1fs", self.stats.files, self.stats.dirs, self.stats.last_run_seconds)
        if self.index_content:
            self.index_content_sync()
        return self.stats

    @staticmethod
    def _flush(db: sqlite3.Connection, batch: List[Tuple]) -> None:
        if not batch:
            return
        db.executemany(
            "INSERT INTO files(path,name,name_lower,ext,parent,size,mtime,ctime,is_dir,run) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET name=excluded.name, name_lower=excluded.name_lower, ext=excluded.ext, "
            "parent=excluded.parent, size=excluded.size, mtime=excluded.mtime, ctime=excluded.ctime, is_dir=excluded.is_dir, run=excluded.run",
            batch,
        )
        db.commit()

    async def scan(self, roots: Optional[Iterable[Path]] = None) -> IndexStats:
        return await asyncio.get_running_loop().run_in_executor(None, self.scan_sync, roots)

    def start_background_scan(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self.scan())

    async def ensure_scanned(self, timeout: float = 0) -> None:
        """If a scan is running wait up to timeout seconds; if never scanned, scan now."""
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout) if timeout else None
            except asyncio.TimeoutError:
                pass
            return
        if self.stats.last_run_at == 0 and self.count() == 0:
            await self.scan()

    def count(self) -> int:
        with self._lock:
            return int(self._db().execute("SELECT COUNT(*) FROM files WHERE is_dir=0").fetchone()[0])

    # ---------------------------------------------------------- content
    def extract_text(self, path: str, ext: str) -> Optional[str]:
        try:
            if ext in TEXT_EXTS:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read(MAX_CONTENT_CHARS)
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader  # type: ignore
                except ImportError:
                    return None
                reader = PdfReader(path)
                parts = []
                total = 0
                for page in reader.pages[:200]:
                    t = page.extract_text() or ""
                    parts.append(t)
                    total += len(t)
                    if total > MAX_CONTENT_CHARS:
                        break
                return "\n".join(parts)[:MAX_CONTENT_CHARS]
            if ext == ".docx":
                try:
                    import docx  # type: ignore
                except ImportError:
                    return None
                d = docx.Document(path)
                return "\n".join(p.text for p in d.paragraphs)[:MAX_CONTENT_CHARS]
            if ext == ".xlsx":
                try:
                    import openpyxl  # type: ignore
                except ImportError:
                    return None
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                parts = []
                total = 0
                for ws in wb.worksheets[:20]:
                    parts.append(f"# {ws.title}")
                    for row in ws.iter_rows(values_only=True):
                        line = "\t".join("" if v is None else str(v) for v in row)
                        parts.append(line)
                        total += len(line)
                        if total > MAX_CONTENT_CHARS:
                            break
                return "\n".join(parts)[:MAX_CONTENT_CHARS]
            if ext == ".pptx":
                try:
                    from pptx import Presentation  # type: ignore
                except ImportError:
                    return None
                prs = Presentation(path)
                parts = []
                for slide in prs.slides:
                    for shape in slide.shapes:
                        if hasattr(shape, "text"):
                            parts.append(shape.text)
                return "\n".join(parts)[:MAX_CONTENT_CHARS]
        except Exception as e:  # noqa: BLE001
            log.debug("extract failed for %s: %s", path, e.__class__.__name__)
        return None

    def index_content_sync(self, limit: int = 5000) -> int:
        if not self._fts_available:
            return 0
        exts = tuple(TEXT_EXTS | DOC_EXTS)
        with self._lock:
            db = self._db()
            placeholders = ",".join("?" for _ in exts)
            rows = db.execute(
                f"SELECT path, ext, mtime FROM files WHERE is_dir=0 AND size<=? AND size>0 AND ext IN ({placeholders}) "
                "AND (content_mtime IS NULL OR content_mtime < mtime) LIMIT ?",
                (self.content_max_bytes, *exts, limit),
            ).fetchall()
        n = 0
        for path, ext, mtime in rows:
            text = self.extract_text(path, ext)
            with self._lock:
                db = self._db()
                db.execute("DELETE FROM content WHERE path=?", (path,))
                if text and text.strip():
                    db.execute("INSERT INTO content(path, body) VALUES (?,?)", (path, text))
                    n += 1
                db.execute("UPDATE files SET content_mtime=? WHERE path=?", (mtime, path))
                if n % 200 == 0:
                    db.commit()
        with self._lock:
            self._db().commit()
            self.stats.content_indexed = int(self._db().execute("SELECT COUNT(*) FROM content").fetchone()[0])
        log.info("Content index updated: %d documents (re)indexed", n)
        return n

    # ----------------------------------------------------------- search
    def search_sync(self, *, query: str = "", extensions: Optional[List[str]] = None, directory: Optional[str] = None,
                    modified_after: Optional[float] = None, modified_before: Optional[float] = None,
                    created_after: Optional[float] = None, created_before: Optional[float] = None,
                    min_size: Optional[int] = None, max_size: Optional[int] = None, content: str = "",
                    include_dirs: bool = False, sort: str = "relevance", limit: int = 25) -> List[Dict[str, Any]]:
        where, params = [], []
        if not include_dirs:
            where.append("f.is_dir=0")
        tokens = [t for t in re.split(r"\s+", query.strip().lower()) if t] if query else []
        for t in tokens:
            where.append("f.name_lower LIKE ?")
            params.append(f"%{t}%")
        if extensions:
            exts = [("." + e.lower().lstrip(".")) for e in extensions]
            where.append("f.ext IN (%s)" % ",".join("?" for _ in exts))
            params.extend(exts)
        if directory:
            d = str(Path(directory)).rstrip("\\/")
            where.append("(f.path LIKE ? OR f.parent = ?)")
            params.extend([d + os.sep + "%", d])
        if modified_after is not None:
            where.append("f.mtime >= ?"); params.append(modified_after)
        if modified_before is not None:
            where.append("f.mtime <= ?"); params.append(modified_before)
        if created_after is not None:
            where.append("f.ctime >= ?"); params.append(created_after)
        if created_before is not None:
            where.append("f.ctime <= ?"); params.append(created_before)
        if min_size is not None:
            where.append("f.size >= ?"); params.append(min_size)
        if max_size is not None:
            where.append("f.size <= ?"); params.append(max_size)

        select_extra, join, order = "", "", ""
        if content and self._fts_available:
            join = "JOIN content c ON c.path = f.path"
            where.append("content MATCH ?")
            params.append(_fts_query(content))
            select_extra = ", snippet(content, 1, '[', ']', '…', 12) AS snippet, bm25(content) AS rank"
            order = "ORDER BY rank"
        elif content:
            return []
        if sort == "modified":
            order = "ORDER BY f.mtime DESC"
        elif sort == "size":
            order = "ORDER BY f.size DESC"
        elif sort == "name":
            order = "ORDER BY f.name_lower ASC"
        elif not order:
            order = "ORDER BY f.mtime DESC"
        sql = f"SELECT f.path, f.name, f.ext, f.size, f.mtime, f.ctime, f.is_dir{select_extra} FROM files f {join}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" {order} LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._lock:
            rows = self._db().execute(sql, params).fetchall()
        out = []
        for r in rows:
            item = {"path": r[0], "name": r[1], "ext": r[2], "size": r[3], "modified": _iso(r[4]), "created": _iso(r[5]),
                    "is_dir": bool(r[6])}
            if select_extra:
                item["snippet"] = r[7]
            out.append(item)
        if tokens and sort == "relevance" and not content:
            # boost exact/prefix name matches
            q = " ".join(tokens)
            out.sort(key=lambda i: (0 if i["name"].lower() == q else 1 if i["name"].lower().startswith(q) else 2, -_ts(i["modified"])))
        return out

    async def search(self, **kw: Any) -> List[Dict[str, Any]]:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: self.search_sync(**kw))

    async def semantic_search(self, query: str, limit: int = 10) -> Tuple[List[Dict[str, Any]], str]:
        """Embedding-based search over indexed content. Returns (results, mode)."""
        if self.embedder is None:
            res = await self.search(content=query, limit=limit)
            return res, "fulltext (no embedding provider configured)"
        try:
            qvec = (await self.embedder.embed([query], model=self.embedding_model))[0]
        except Exception as e:  # noqa: BLE001
            res = await self.search(content=query, limit=limit)
            return res, f"fulltext (embedding failed: {e.__class__.__name__})"
        await asyncio.get_running_loop().run_in_executor(None, self._ensure_embeddings)
        with self._lock:
            rows = self._db().execute("SELECT path, name, ext, size, mtime, embedding FROM files WHERE embedding IS NOT NULL").fetchall()
        scored = []
        for path, name, ext, size, mtime, emb in rows:
            try:
                vec = json.loads(emb)
            except Exception:  # noqa: BLE001
                continue
            scored.append((_cos(qvec, vec), path, name, ext, size, mtime))
        scored.sort(reverse=True)
        return [{"path": p, "name": n, "ext": e, "size": s, "modified": _iso(m), "score": round(sc, 3)}
                for sc, p, n, e, s, m in scored[:limit]], "semantic"

    def _ensure_embeddings(self, batch: int = 200) -> None:
        """Embed content for files lacking an embedding (bounded per call)."""
        if self.embedder is None or not self._fts_available:
            return
        with self._lock:
            rows = self._db().execute(
                "SELECT f.path, substr(c.body, 1, ?) FROM files f JOIN content c ON c.path=f.path WHERE f.embedding IS NULL LIMIT ?",
                (EMBED_CHARS, batch)).fetchall()
        if not rows:
            return
        try:
            loop = asyncio.new_event_loop()
            vecs = loop.run_until_complete(self.embedder.embed([r[1] for r in rows], model=self.embedding_model))
            loop.close()
        except Exception as e:  # noqa: BLE001
            log.warning("content embedding failed: %s", e.__class__.__name__)
            return
        with self._lock:
            db = self._db()
            db.executemany("UPDATE files SET embedding=? WHERE path=?", [(json.dumps(v), r[0]) for v, r in zip(vecs, rows)])
            db.commit()

    def status(self) -> Dict[str, Any]:
        return {"files": self.stats.files, "dirs": self.stats.dirs, "content_indexed": self.stats.content_indexed,
                "last_run_seconds": self.stats.last_run_seconds, "last_run_at": _iso(self.stats.last_run_at) if self.stats.last_run_at else None,
                "running": self.stats.running or (self._task is not None and not self._task.done()),
                "roots": [str(r) for r in self.roots], "fts": self._fts_available, "semantic": self.embedder is not None}


def _fts_query(text: str) -> str:
    # Quote each token to avoid FTS syntax errors from user text; AND semantics.
    toks = [t for t in re.findall(r"[\w\-]+", text) if t]
    return " ".join(f'"{t}"' for t in toks) or '""'


def _iso(ts: float) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return ""


def _ts(iso: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _cos(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
