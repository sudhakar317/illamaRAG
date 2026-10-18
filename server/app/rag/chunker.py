"""Splitting documents into retrievable chunks.

Chunking quality sets a ceiling on retrieval quality, so this does a little
more than slice every N characters:

- It never merges across a page or section boundary, so a chunk always has one
  honest citation rather than straddling two.
- It splits on paragraph breaks first, then sentences, and only falls back to
  a hard character cut for text that offers neither. A chunk cut mid-sentence
  embeds poorly, because the vector ends up describing a fragment.
- Consecutive chunks overlap, so a fact sitting on a boundary still appears
  whole in at least one of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.loaders import LoadedDocument

# A sentence end followed by whitespace. Deliberately simple: abbreviations
# occasionally fool it, which costs a slightly odd split and nothing more.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH = re.compile(r"\n\s*\n")


@dataclass
class Chunk:
    text: str
    index: int  # position within the document, used to pull in neighbours
    page: int | None = None
    section: str | None = None


def _split_long(text: str, limit: int) -> list[str]:
    """Break text that exceeds `limit`, preferring sentence boundaries."""
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        if current and len(current) + len(sentence) + 1 > limit:
            pieces.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence

        # A single sentence longer than the limit (tables, generated text,
        # minified content) still has to be cut somewhere.
        while len(current) > limit:
            pieces.append(current[:limit].strip())
            current = current[limit:]

    if current.strip():
        pieces.append(current.strip())
    return pieces


def _tail_overlap(text: str, overlap: int) -> str:
    """The last `overlap` characters, trimmed to start at a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text if overlap > 0 else ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def chunk_document(
    document: LoadedDocument,
    chunk_chars: int = 1200,
    overlap_chars: int = 200,
) -> list[Chunk]:
    chunks: list[Chunk] = []

    for block in document.blocks:
        units: list[str] = []
        for paragraph in _PARAGRAPH.split(block.text):
            paragraph = paragraph.strip()
            if paragraph:
                units.extend(_split_long(paragraph, chunk_chars))

        # Pack units into chunks up to the size limit, carrying an overlap
        # from the previous chunk so boundaries are not hard cuts.
        current = ""
        for unit in units:
            if current and len(current) + len(unit) + 2 > chunk_chars:
                chunks.append(
                    Chunk(
                        text=current.strip(),
                        index=len(chunks),
                        page=block.page,
                        section=block.section,
                    )
                )
                carry = _tail_overlap(current, overlap_chars)
                current = f"{carry}\n\n{unit}" if carry else unit
            else:
                current = f"{current}\n\n{unit}" if current else unit

        if current.strip():
            chunks.append(
                Chunk(
                    text=current.strip(),
                    index=len(chunks),
                    page=block.page,
                    section=block.section,
                )
            )

    # Very short trailing fragments carry almost no meaning on their own and
    # dilute the index, so fold them back into the previous chunk.
    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk.text) < 120
            and merged[-1].page == chunk.page
            and merged[-1].section == chunk.section
            and len(merged[-1].text) + len(chunk.text) < chunk_chars * 1.4
        ):
            merged[-1].text = f"{merged[-1].text}\n\n{chunk.text}"
            continue
        chunk.index = len(merged)
        merged.append(chunk)

    return merged


def contextualise(chunk: Chunk, document_title: str) -> str:
    """Prefix a chunk with its document and section before embedding.

    A chunk lifted out of its document loses the context that made it
    meaningful — "the limit is 30 days" says nothing about which policy. Naming
    the source inside the embedded text keeps that association in the vector,
    and noticeably improves retrieval for documents full of similar-sounding
    passages.
    """
    parts = [document_title]
    if chunk.section:
        parts.append(chunk.section)
    elif chunk.page is not None:
        parts.append(f"page {chunk.page}")
    return f"{' — '.join(parts)}\n\n{chunk.text}"
