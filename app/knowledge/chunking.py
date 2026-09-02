"""
Deterministic, section-aware chunking.

Design points that matter for retrieval quality:

* Chunks are built *within* a section (a PDF page, a markdown heading, a block
  of CSV rows), never across two of them. A chunk therefore always has one
  honest answer to "where is this from?", and a heading never gets welded onto
  unrelated text from the next page.
* A heading is never left stranded at the end of a chunk. If a split would
  land just after a heading line, the split point moves back so the heading
  travels with the text it introduces.
* Splits prefer paragraph breaks, then sentence ends, then whitespace. Cutting
  mid-word produces a token sequence that exists in no real document and
  embeds poorly.
* The function is pure: same text plus same settings gives byte-identical
  chunks, every time. Reindexing a document that has not changed must produce
  the same rows, which is what makes retries idempotent.

Sizes are in characters, not tokens. Tokenization is provider-specific, and a
character budget with a 4:1 estimate keeps chunking independent of whichever
embedding model is configured. The default 3200 characters is roughly 800
tokens, comfortably inside every provider's input window.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.knowledge.extractors import Section, estimate_tokens

#: Split preference, strongest first. Each is searched for in the tail window
#: of the candidate chunk.
_PARAGRAPH_BREAK = "\n\n"
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")
_LINE_BREAK = "\n"

#: A markdown heading or a short title-like line that must not be orphaned.
_HEADING_LINE = re.compile(r"^\s{0,3}(#{1,6}\s+\S.*|[A-Z][^\n]{0,70}:)\s*$")

#: Fraction of the chunk searched backwards for a clean boundary. Looking
#: further back than this wastes more capacity than a clean split is worth.
_BOUNDARY_WINDOW = 0.35


@dataclass
class Chunk:
    text: str
    index: int
    #: Copied from the source section: {"page": 3} / {"heading": "Refunds"}.
    metadata: dict = field(default_factory=dict)
    char_count: int = 0
    token_estimate: int = 0

    def __post_init__(self) -> None:
        self.char_count = len(self.text)
        self.token_estimate = estimate_tokens(self.text)


@dataclass(frozen=True)
class ChunkingConfig:
    size: int = 3200
    overlap: int = 400
    #: Fragments shorter than this are merged into a neighbour rather than
    #: stored. A 30-character chunk is noise that pollutes top-k.
    min_size: int = 120

    def validated(self) -> "ChunkingConfig":
        size = max(200, int(self.size))
        overlap = max(0, min(int(self.overlap), size // 2))
        min_size = max(1, min(int(self.min_size), size))
        return ChunkingConfig(size=size, overlap=overlap, min_size=min_size)


def config_from_settings(settings) -> ChunkingConfig:
    """Build a config from app settings, tolerating older setting sets."""
    return ChunkingConfig(
        size=getattr(settings, "knowledge_chunk_chars", 3200),
        overlap=getattr(settings, "knowledge_chunk_overlap_chars", 400),
        min_size=getattr(settings, "knowledge_min_chunk_chars", 120),
    ).validated()


def _find_split(text: str, limit: int, window: int) -> int:
    """
    Choose where to end a chunk that may be at most `limit` characters.

    Searches backwards from `limit` for the cleanest boundary available within
    `window` characters. Returns `limit` when nothing better exists, which
    only happens for text with no whitespace at all.
    """
    if len(text) <= limit:
        return len(text)

    floor = max(1, limit - window)
    head = text[:limit]

    paragraph = head.rfind(_PARAGRAPH_BREAK, floor)
    if paragraph != -1:
        return paragraph + len(_PARAGRAPH_BREAK)

    sentence = None
    for match in _SENTENCE_END.finditer(head, floor):
        sentence = match.end()
    if sentence is not None:
        return sentence

    line = head.rfind(_LINE_BREAK, floor)
    if line != -1:
        return line + 1

    space = head.rfind(" ", floor)
    if space != -1:
        return space + 1

    return limit


def _pull_back_orphan_heading(text: str, split: int) -> int:
    """
    Stop a chunk from ending on a heading.

    If the last non-empty line before the split looks like a heading, move the
    split to before that line so the heading stays attached to its body.
    """
    head = text[:split]
    lines = head.split("\n")
    # Ignore trailing blank lines produced by a paragraph-break split.
    tail_index = len(lines) - 1
    while tail_index >= 0 and not lines[tail_index].strip():
        tail_index -= 1
    if tail_index <= 0:
        return split

    if not _HEADING_LINE.match(lines[tail_index]):
        return split

    consumed = sum(len(line) + 1 for line in lines[:tail_index])
    # Only pull back if a usable chunk remains; otherwise keep the heading.
    return consumed if consumed > 0 else split


def _overlap_start(text: str, end: int, overlap: int) -> int:
    """
    Where the next chunk begins.

    Overlap is aligned to a word boundary so the repeated span reads as
    language, and is always at least one character forward to guarantee
    termination.
    """
    if overlap <= 0:
        return end
    start = max(0, end - overlap)
    space = text.find(" ", start, end)
    if space != -1:
        start = space + 1
    return start if start < end else end


def _split_section(text: str, config: ChunkingConfig) -> list[str]:
    """Split one section's text into size-bounded pieces."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= config.size:
        return [text]

    window = max(1, int(config.size * _BOUNDARY_WINDOW))
    pieces: list[str] = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        if len(remaining) <= config.size:
            pieces.append(remaining.strip())
            break

        split = _find_split(remaining, config.size, window)
        split = _pull_back_orphan_heading(remaining, split)
        piece = remaining[:split].strip()
        if piece:
            pieces.append(piece)

        advance = _overlap_start(remaining, split, config.overlap)
        if advance <= 0:
            advance = split          # defensive: never loop forever
        cursor += advance

    return [p for p in pieces if p]


def _merge_small(pieces: list[str], min_size: int, limit: int) -> list[str]:
    """Fold undersized fragments into the previous piece when they fit."""
    merged: list[str] = []
    for piece in pieces:
        if (
            merged
            and len(piece) < min_size
            and len(merged[-1]) + len(piece) + 2 <= limit
        ):
            merged[-1] = f"{merged[-1]}\n\n{piece}"
        else:
            merged.append(piece)
    return merged


def chunk_sections(
    sections: list[Section], config: ChunkingConfig | None = None
) -> list[Chunk]:
    """
    Chunk a list of extracted sections.

    Section metadata (page number, heading) is carried onto every chunk
    produced from that section, and `part`/`part_count` are added when a
    section had to be split so a citation can say "page 3, part 2 of 3".
    """
    config = (config or ChunkingConfig()).validated()

    chunks: list[Chunk] = []
    index = 0
    for section in sections:
        pieces = _merge_small(
            _split_section(section.text, config), config.min_size, config.size
        )
        # A section that is nothing but a heading carries no information on its
        # own, so it is dropped rather than embedded as a bare title.
        #
        # The test is deliberately narrow -- it must match the heading pattern
        # exactly. An earlier version also dropped any short single-line
        # section, which silently discarded real content: a PDF page reading
        # "Parking is free in the lot behind our building." is short, has no
        # newline, and is exactly the fact a caller rings up to ask about.
        # Losing content is a far worse failure than embedding a stray title.
        if (
            len(pieces) == 1
            and len(sections) > 1
            and len(pieces[0]) < config.min_size
            and _HEADING_LINE.match(pieces[0].strip())
        ):
            continue

        for part, piece in enumerate(pieces, start=1):
            metadata = dict(section.metadata)
            if len(pieces) > 1:
                metadata["part"] = part
                metadata["part_count"] = len(pieces)
            chunks.append(Chunk(text=piece, index=index, metadata=metadata))
            index += 1

    return chunks


def chunk_text(text: str, config: ChunkingConfig | None = None) -> list[Chunk]:
    """Convenience wrapper for text with no section structure."""
    return chunk_sections([Section(text=text, metadata={})], config)

