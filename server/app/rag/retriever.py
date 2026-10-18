"""Hybrid retrieval: vector search and BM25, fused, filtered and expanded.

The pipeline, in order:

1. **Contextualise the query.** "What about the second one?" is unanswerable on
   its own, so a short or anaphoric follow-up is rewritten using the previous
   turn before anything is searched.
2. **Search both indexes** independently for candidates.
3. **Fuse with Reciprocal Rank Fusion.** RRF combines rankings rather than
   scores, which matters because cosine similarity and BM25 are on different,
   unnormalised scales — any weighted sum of the two needs constant retuning,
   while RRF needs none.
4. **Filter.** A chunk must be semantically close enough, or a strong literal
   keyword match. Everything else is dropped, and if nothing survives the
   caller is told there is no context — far better than answering confidently
   from three irrelevant passages.
5. **Expand to neighbours.** An answer that straddles a chunk boundary is
   completed by pulling in the adjacent chunks.
6. **Budget.** Context is capped so documents cannot crowd the conversation
   out of a 4096-token window.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.rag import embedder, store

logger = logging.getLogger(__name__)

# Standard RRF constant. It damps the influence of top ranks so that one index
# being very confident cannot alone decide the outcome.
RRF_K = 60

# A follow-up this short almost certainly depends on the previous turn.
_SHORT_QUERY_WORDS = 6

_ANAPHORA = re.compile(
    r"\b(it|its|that|this|those|these|they|them|their|he|she|his|her|"
    r"the former|the latter|same|above|previous)\b",
    re.IGNORECASE,
)


@dataclass
class Retrieved:
    chunk_id: int
    text: str
    title: str
    source_type: str
    source: str | None
    page: int | None
    section: str | None
    document_id: str
    score: float
    dense_score: float | None = None
    keyword_score: float | None = None
    is_neighbour: bool = False

    def citation(self) -> str:
        parts = [self.title]
        if self.section:
            parts.append(self.section)
        elif self.page is not None:
            parts.append(f"p.{self.page}")
        return " — ".join(parts)


def contextualise_query(message: str, history: list[dict]) -> str:
    """Fold the previous turn into a follow-up question.

    Retrieval sees one query with no memory, so "and the second one?" would
    otherwise be matched against the documents literally and return nothing
    useful. Done with a heuristic rather than an LLM rewrite on purpose: a
    rewrite call would cost several seconds per message at 4-6 tokens/sec.
    """
    text = message.strip()
    words = text.split()
    needs_context = len(words) <= _SHORT_QUERY_WORDS or bool(_ANAPHORA.search(text))
    if not needs_context or not history:
        return text

    previous = next(
        (m["content"] for m in reversed(history) if m["role"] == "user"), None
    )
    if not previous:
        return text

    # Only the previous question is prepended, not the assistant's answer: the
    # answer is long and would swamp the query's own terms.
    return f"{previous.strip()[:300]}\n{text}"


def _rrf(ranked_lists: list[list[tuple[int, float]]]) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk_id, _) in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def retrieve(
    query: str,
    history: list[dict] | None = None,
    top_k: int | None = None,
) -> list[Retrieved]:
    """Find the most relevant chunks for a query. Empty list means none."""
    documents, chunks = store.count_documents()
    if chunks == 0:
        return []

    top_k = top_k or settings.rag_top_k
    search_query = contextualise_query(query, history or [])
    candidates = settings.rag_candidates

    dense = store.dense_search(embedder.embed_query(search_query), candidates)
    keyword = store.keyword_search(search_query, candidates)

    if not dense and not keyword:
        return []

    dense_scores = dict(dense)
    keyword_scores = dict(keyword)
    # Literal matches ranked this highly are trusted even when the vector
    # score is low: that is exactly the case vectors handle badly (names,
    # codes, version numbers).
    strong_keyword = {cid for cid, _ in keyword[:3]}

    fused = _rrf([dense, keyword])

    kept: list[tuple[int, float]] = []
    for chunk_id, score in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
        similarity = dense_scores.get(chunk_id)
        relevant = (
            similarity is not None and similarity >= settings.rag_min_score
        ) or chunk_id in strong_keyword
        if relevant:
            kept.append((chunk_id, score))
        if len(kept) >= top_k:
            break

    if not kept:
        logger.info(
            "No chunk cleared the relevance floor (best cosine %.3f, floor %.2f).",
            max(dense_scores.values()) if dense_scores else 0.0,
            settings.rag_min_score,
        )
        return []

    rows = store.get_chunks([cid for cid, _ in kept])
    results: list[Retrieved] = []
    seen_texts: set[str] = set()

    for chunk_id, score in kept:
        row = rows.get(chunk_id)
        if row is None:
            continue
        fingerprint = row.text[:200]
        if fingerprint in seen_texts:
            continue
        seen_texts.add(fingerprint)
        results.append(
            Retrieved(
                chunk_id=row.id,
                text=row.text,
                title=row.title,
                source_type=row.source_type,
                source=row.source,
                page=row.page,
                section=row.section,
                document_id=str(row.document_id),
                score=score,
                dense_score=dense_scores.get(chunk_id),
                keyword_score=keyword_scores.get(chunk_id),
            )
        )

    # Neighbours for the strongest hits only; doing it for every result would
    # triple the context for little gain.
    for row in list(results[:2]):
        for neighbour in store.get_neighbours(
            row.document_id, _chunk_idx(row.chunk_id), settings.rag_neighbour_window
        ):
            fingerprint = neighbour.text[:200]
            if fingerprint in seen_texts:
                continue
            seen_texts.add(fingerprint)
            results.append(
                Retrieved(
                    chunk_id=neighbour.id,
                    text=neighbour.text,
                    title=neighbour.title,
                    source_type=neighbour.source_type,
                    source=neighbour.source,
                    page=neighbour.page,
                    section=neighbour.section,
                    document_id=str(neighbour.document_id),
                    score=row.score * 0.5,
                    is_neighbour=True,
                )
            )

    return results


def _chunk_idx(chunk_id: int) -> int:
    rows = store.get_chunks([chunk_id])
    row = rows.get(chunk_id)
    return row.idx if row else 0


def build_context(results: list[Retrieved]) -> tuple[str, list[dict]]:
    """Render retrieved chunks as prompt text plus a citation list."""
    if not results:
        return "", []

    blocks: list[str] = []
    sources: list[dict] = []
    used = 0
    seen_citations: set[str] = set()

    for index, item in enumerate(results, start=1):
        snippet = item.text.strip()
        if used + len(snippet) > settings.rag_max_context_chars:
            remaining = settings.rag_max_context_chars - used
            if remaining < 300:
                break
            snippet = snippet[:remaining].rsplit(" ", 1)[0] + "…"

        blocks.append(f"[{index}] {item.citation()}\n{snippet}")
        used += len(snippet)

        citation = item.citation()
        if citation not in seen_citations:
            seen_citations.add(citation)
            sources.append(
                {
                    "n": index,
                    "title": item.title,
                    "citation": citation,
                    "source_type": item.source_type,
                    "source": item.source,
                    "page": item.page,
                    "section": item.section,
                    "document_id": item.document_id,
                    "score": round(item.dense_score, 3)
                    if item.dense_score is not None
                    else None,
                }
            )

    return "\n\n".join(blocks), sources
