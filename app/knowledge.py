"""Clinic knowledge base (FAQ, policies).

Two modes, chosen automatically by size:

* inline    - the whole KB goes into the system prompt. For a small clinic (a few pages of FAQ)
              this is the right answer: zero retrieval latency, no missed chunks, cache-friendly.
* retrieval - when the KB grows past INLINE_LIMIT_CHARS, the agent gets a `search_clinic_info`
              tool backed by a tiny BM25 index. Swap `BM25Index` for embeddings + pgvector
              (or OpenAI vector stores) if you ever have hundreds of documents.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

INLINE_LIMIT_CHARS = 12_000
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class Chunk:
    title: str
    text: str


def _stem(t: str) -> str:
    # Crude suffix stripping so "parking" matches "park"; good enough for a few pages of FAQ.
    for suffix in ("ing", "ed", "es", "s"):
        if len(t) > len(suffix) + 2 and t.endswith(suffix):
            return t[: -len(suffix)]
    return t


def _tokens(s: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(s.lower())]


def load_chunks(directory: str | Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(directory).glob("*.md")):
        title, lines = path.stem, []
        for line in path.read_text("utf-8").splitlines():
            if line.startswith("#"):
                if lines:
                    chunks.append(Chunk(title, "\n".join(lines).strip()))
                title, lines = line.lstrip("# ").strip(), []
            elif line.strip():
                lines.append(line)
        if lines:
            chunks.append(Chunk(title, "\n".join(lines).strip()))
    return chunks


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.docs = [_tokens(c.title + " " + c.text) for c in chunks]
        self.avgdl = sum(map(len, self.docs)) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 3) -> list[Chunk]:
        q = _tokens(query)
        scored = []
        for chunk, doc in zip(self.chunks, self.docs):
            tf = Counter(doc)
            score = sum(
                self.idf.get(t, 0) * tf[t] * (self.k1 + 1)
                / (tf[t] + self.k1 * (1 - self.b + self.b * len(doc) / self.avgdl))
                for t in q
                if t in tf
            )
            if score > 0:
                scored.append((score, chunk))
        return [c for _, c in sorted(scored, key=lambda x: -x[0])[:k]]


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.full_text = "\n\n".join(f"## {c.title}\n{c.text}" for c in chunks)
        self.inline = len(self.full_text) <= INLINE_LIMIT_CHARS
        self.index = None if self.inline else BM25Index(chunks)

    def search(self, query: str, k: int = 3) -> list[dict]:
        index = self.index or BM25Index(self.chunks)
        return [{"title": c.title, "text": c.text} for c in index.search(query, k)]


@lru_cache
def get_kb() -> KnowledgeBase:
    return KnowledgeBase(load_chunks(get_settings().knowledge_dir))
