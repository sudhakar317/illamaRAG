"""The agent's tools. There is one, on purpose: kb_search.

Kept as a registry from the start so that adding a tool later means adding an
entry here and a way to choose it inside RESOLVE, with the claim tree, scoring
and budget untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.rag import retriever

# Caps keep a tool's output inside what a 4096-token prompt can carry.
MAX_HITS = 4
HIT_CHARS = 500


@dataclass
class Hit:
    chunk_id: int
    text: str
    title: str
    citation: str
    source_type: str
    source: str | None
    page: int | None
    section: str | None
    document_id: str
    score: float | None  # cosine similarity, when the vector side found it

    def as_source(self) -> dict:
        return {
            "title": self.title,
            "citation": self.citation,
            "source_type": self.source_type,
            "source": self.source,
            "page": self.page,
            "section": self.section,
            "document_id": self.document_id,
            "score": round(self.score, 3) if self.score is not None else None,
        }


def kb_search(query: str, top_k: int = MAX_HITS) -> list[Hit]:
    """Hybrid search over the knowledge base. Empty means nothing relevant."""
    results = retriever.retrieve(query, history=[], top_k=top_k)
    hits = []
    for r in results:
        # Neighbouring chunks pad an answer out; for judging a claim they only
        # dilute the passage that actually matched.
        if r.is_neighbour:
            continue
        hits.append(
            Hit(
                chunk_id=r.chunk_id,
                text=r.text.strip()[:HIT_CHARS],
                title=r.title,
                citation=r.citation(),
                source_type=r.source_type,
                source=r.source,
                page=r.page,
                section=r.section,
                document_id=r.document_id,
                score=r.dense_score,
            )
        )
    return hits[:top_k]


@dataclass
class Tool:
    name: str
    description: str
    run: Callable[..., list[Hit]]


TOOLS = {
    "kb_search": Tool(
        name="kb_search",
        description="Search the knowledge base for passages relevant to a query.",
        run=kb_search,
    ),
}
