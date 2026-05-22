"""Corpus retrieval: cited passages for an interpretation.

Offline lexical retrieval (BM25) over ingested chunks — no embeddings, no
network, runs in CI. The scoring backend (`_BM25`) is isolated so an
embedding backend can replace it later without touching callers.

Only APPROVED sources are retrievable.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from sca import config
from sca.corpus.sources import approved_sources
from sca.models import CorpusPassage

_TOKEN_RE = re.compile(r"[a-z0-9§]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class _BM25:
    """Minimal BM25 ranker. Swap for an embedding backend behind retrieve()."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for doc in docs:
            for term in set(doc):
                self.df[term] += 1

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.n - n + 0.5) / (n + 0.5))

    def score(self, query: list[str], idx: int) -> float:
        doc = self.docs[idx]
        if not doc:
            return 0.0
        freqs = Counter(doc)
        dl = len(doc)
        total = 0.0
        for term in query:
            tf = freqs.get(term, 0)
            if not tf:
                continue
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            total += self._idf(term) * tf * (self.k1 + 1) / denom
        return total


def _load_chunks(store: Path | None) -> list[dict]:
    """Load ingested chunks for APPROVED sources only.

    With `store` omitted, passages come from the durable `Store`
    (`get_passages`) — production reads from Supabase via config alone. An
    explicit `store` directory keeps the legacy on-disk path (hermetic tests).
    The approved-sources gate is enforced either way.
    """
    if store is None:
        from sca.store import get_store

        backing = get_store()
        chunks: list[dict] = []
        for src in approved_sources():
            chunks.extend(backing.get_passages(src.id))
        return chunks
    out_dir = store
    chunks = []
    for src in approved_sources():
        path = out_dir / f"{src.id}.json"
        if path.exists():
            chunks.extend(json.loads(path.read_text()))
    return chunks


def retrieve(
    query: str, *, k: int = 6, store: Path | None = None
) -> list[CorpusPassage]:
    """Return up to k relevant passages from APPROVED, ingested sources."""
    chunks = _load_chunks(store)
    if not chunks:
        return []
    docs = [_tokenize(c["text"] + " " + c["heading"]) for c in chunks]
    bm25 = _BM25(docs)
    q = _tokenize(query)
    ranked = sorted(
        ((bm25.score(q, i), i) for i in range(len(chunks))),
        key=lambda t: t[0],
        reverse=True,
    )
    passages: list[CorpusPassage] = []
    for score, i in ranked[:k]:
        if score <= 0:
            continue
        c = chunks[i]
        passages.append(
            CorpusPassage(
                source_id=c["source_id"],
                section=c["section"],
                heading=c["heading"],
                page=c.get("page"),
                text=c["text"],
                citation=f"{c['source_id']} §{c['section']}",
                score=score,
            )
        )
    return passages
