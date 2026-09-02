"""Plain text and Markdown."""
from __future__ import annotations

import re

from app.knowledge.extractors.base import (
    ExtractedDocument,
    ExtractionError,
    Section,
    clean_text,
)

#: ATX (`## Heading`) and setext (`Heading\n=====`) markdown headings.
_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.M)
_SETEXT = re.compile(r"^(?P<title>[^\n]+)\n(?P<rule>={3,}|-{3,})$", re.M)


def _decode(data: bytes) -> str:
    """
    Decode without guessing wildly.

    UTF-8 first, then UTF-16 (Windows exports), then latin-1, which cannot
    fail. Anything undecodable becomes a replacement character rather than an
    exception, because a single bad byte should not lose a whole price list.
    """
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class TextExtractor:
    """Handles .txt and .md. Markdown headings become section metadata."""

    def extract(self, data: bytes, *, filename: str | None = None) -> ExtractedDocument:
        if not data:
            raise ExtractionError("the file is empty")

        text = clean_text(_decode(data))
        if not text:
            raise ExtractionError("no readable text found in the file")

        return ExtractedDocument(
            text=text,
            sections=self._split_by_heading(text),
            metadata={"format": "markdown" if _looks_markdown(text) else "text"},
        )

    def _split_by_heading(self, text: str) -> list[Section]:
        """
        Split on markdown headings so chunking can avoid cutting a section in
        half. A document with no headings is one section.
        """
        matches = list(_ATX.finditer(text))
        if not matches:
            return [Section(text=text, metadata={})]

        sections: list[Section] = []
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(Section(text=preamble, metadata={}))

        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.start():end].strip()
            if body:
                sections.append(
                    Section(
                        text=body,
                        metadata={
                            "heading": match.group(2).strip(),
                            "level": len(match.group(1)),
                        },
                    )
                )
        return sections


def _looks_markdown(text: str) -> bool:
    return bool(_ATX.search(text) or _SETEXT.search(text) or "```" in text)

