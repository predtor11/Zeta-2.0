"""Long-term memory: user preferences and durable facts.

SQLite-backed.  Retrieval uses keyword overlap scoring by default and
cosine similarity over embeddings when an embedding provider is configured
(EMBEDDING_PROVIDER=ollama|openai|...).  A vector database (Chroma/FAISS/
Qdrant) can replace `_rank` without touching the API.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, or_, select

from app.core.database import session_scope
from app.models.db import MemoryItem
from app.providers.llm.base import LLMProvider

log = logging.getLogger(__name__)

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are", "my", "i", "me", "that", "this", "it",
         "for", "with", "be", "as", "at", "by", "from", "you", "your", "about", "remember", "know", "what", "do"}


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 1]


def _cos(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class LongTermMemory:
    def __init__(self, embedder: Optional[LLMProvider] = None, embedding_model: str = ""):
        self.embedder = embedder
        self.embedding_model = embedding_model or None

    async def _embed(self, text: str) -> Optional[List[float]]:
        if self.embedder is None:
            return None
        try:
            vecs = await self.embedder.embed([text], model=self.embedding_model)
            return vecs[0] if vecs else None
        except Exception as e:  # noqa: BLE001
            log.warning("embedding failed (%s); falling back to keyword retrieval", e.__class__.__name__)
            return None

    async def add(self, content: str, category: str = "fact", tags: Optional[List[str]] = None,
                  importance: float = 0.5) -> MemoryItem:
        content = content.strip()
        emb = await self._embed(content)
        async with session_scope() as s:
            # De-duplicate near-identical memories
            existing = (await s.execute(select(MemoryItem).where(MemoryItem.content == content))).scalar_one_or_none()
            if existing:
                existing.updated_at = datetime.now(timezone.utc)
                return existing
            item = MemoryItem(content=content, category=category, tags=",".join(tags or []),
                              importance=max(0.0, min(1.0, importance)), embedding=emb)
            s.add(item)
            await s.flush()
            return item

    async def update(self, memory_id: str, content: Optional[str] = None, category: Optional[str] = None,
                     tags: Optional[List[str]] = None, importance: Optional[float] = None) -> Optional[MemoryItem]:
        async with session_scope() as s:
            item = await s.get(MemoryItem, memory_id)
            if item is None:
                return None
            if content is not None:
                item.content = content.strip()
                item.embedding = await self._embed(item.content)
            if category is not None:
                item.category = category
            if tags is not None:
                item.tags = ",".join(tags)
            if importance is not None:
                item.importance = max(0.0, min(1.0, importance))
            item.updated_at = datetime.now(timezone.utc)
            return item

    async def delete(self, memory_id: str) -> bool:
        async with session_scope() as s:
            item = await s.get(MemoryItem, memory_id)
            if item is None:
                return False
            await s.delete(item)
            return True

    async def forget_matching(self, query: str) -> int:
        """Delete every memory mentioning the query (case-insensitive substring or strong token overlap)."""
        q = query.strip()
        if not q:
            return 0
        async with session_scope() as s:
            rows = (await s.execute(select(MemoryItem).where(or_(MemoryItem.content.ilike(f"%{q}%"),
                                                                 MemoryItem.tags.ilike(f"%{q}%"))))).scalars().all()
            ids = [r.id for r in rows]
            if not ids:
                # token-based fallback
                qt = set(_tokens(q))
                all_rows = (await s.execute(select(MemoryItem))).scalars().all()
                for r in all_rows:
                    rt = set(_tokens(r.content))
                    if qt and len(qt & rt) / len(qt) >= 0.6:
                        ids.append(r.id)
            if ids:
                await s.execute(delete(MemoryItem).where(MemoryItem.id.in_(ids)))
            return len(ids)

    async def clear(self) -> int:
        async with session_scope() as s:
            rows = (await s.execute(select(MemoryItem.id))).scalars().all()
            await s.execute(delete(MemoryItem))
            return len(rows)

    async def list(self, limit: int = 200, category: Optional[str] = None) -> List[MemoryItem]:
        async with session_scope() as s:
            stmt = select(MemoryItem).order_by(MemoryItem.updated_at.desc()).limit(limit)
            if category:
                stmt = stmt.where(MemoryItem.category == category)
            return list((await s.execute(stmt)).scalars().all())

    async def get(self, memory_id: str) -> Optional[MemoryItem]:
        async with session_scope() as s:
            return await s.get(MemoryItem, memory_id)

    async def search(self, query: str, limit: int = 5, min_score: float = 0.05) -> List[Dict[str, Any]]:
        rows = await self.list(limit=1000)
        if not rows:
            return []
        qvec = await self._embed(query)
        qt = set(_tokens(query))
        scored = []
        for r in rows:
            score = 0.0
            if qvec is not None and r.embedding:
                score = _cos(qvec, r.embedding)
            elif qt:
                rt = set(_tokens(r.content)) | set(_tokens(r.tags))
                if rt:
                    overlap = len(qt & rt)
                    score = overlap / math.sqrt(len(qt) * len(rt)) if overlap else 0.0
            score = score * (0.7 + 0.3 * r.importance)
            if score >= min_score:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._to_dict(r, score) for score, r in scored[:limit]]

    async def context_block(self, query: str, limit: int = 6, always_include_preferences: bool = True) -> str:
        """Compact memory block for the system prompt."""
        items = await self.search(query, limit=limit)
        seen = {i["id"] for i in items}
        if always_include_preferences:
            for r in await self.list(limit=50, category="preference"):
                if r.id not in seen:
                    items.append(self._to_dict(r, 0.0))
                    seen.add(r.id)
        if not items:
            return ""
        lines = [f"- [{i['category']}] {i['content']}" for i in items[:12]]
        return "Known facts and preferences about the user (from long-term memory):\n" + "\n".join(lines)

    @staticmethod
    def _to_dict(r: MemoryItem, score: float = 0.0) -> Dict[str, Any]:
        return {"id": r.id, "content": r.content, "category": r.category,
                "tags": [t for t in r.tags.split(",") if t], "importance": r.importance,
                "created_at": r.created_at, "updated_at": r.updated_at, "score": round(score, 3)}
