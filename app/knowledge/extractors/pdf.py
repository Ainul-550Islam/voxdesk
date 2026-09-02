"""
PDF extraction, page by page.

pypdf is pure Python and does not shell out, which matters: a PDF is an
untrusted file and handing it to a native converter is a much larger attack
surface than reading it in-process.

Page numbers are preserved because "where did that come from?" is the first
question anyone asks about a RAG answer.
"""
from __future__ import annotations

import io

import structlog

from app.knowledge.extractors.base import (
    ExtractedDocument,
    ExtractionError,
    Section,
    clean_text,
)

log = structlog.get_logger()

#: A PDF with more pages than this is almost certainly not a business FAQ.
MAX_PAGES = 500


class PdfExtractor:
    def extract(self, data: bytes, *, filename: str | None = None) -> ExtractedDocument:
        if not data:
            raise ExtractionError("the file is empty")

        try:
            from pypdf import PdfReader
        except ImportError as exc:      # pragma: no cover - deployment issue
            raise ExtractionError("PDF support is not installed") from exc

        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:
            log.warning("extract.pdf_unreadable", error=str(exc))
            raise ExtractionError("this PDF could not be opened") from exc

        if getattr(reader, "is_encrypted", False):
            # Try the empty password, which unlocks the common "protected but
            # not really" case, then give up rather than guessing.
            try:
                if reader.decrypt("") == 0:
                    raise ExtractionError("this PDF is password protected")
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError("this PDF is password protected") from exc

        pages = reader.pages[:MAX_PAGES]
        if not pages:
            raise ExtractionError("this PDF has no pages")

        sections: list[Section] = []
        failed_pages = 0
        for number, page in enumerate(pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception:
                # One broken page must not lose the other ninety-nine.
                failed_pages += 1
                continue
            body = clean_text(raw)
            if body:
                sections.append(Section(text=body, metadata={"page": number}))

        if not sections:
            raise ExtractionError(
                "no text could be extracted -- this PDF may be a scan, "
                "which needs OCR"
            )

        return ExtractedDocument(
            text="\n\n".join(s.text for s in sections),
            sections=sections,
            metadata={
                "format": "pdf",
                "page_count": len(pages),
                "pages_with_text": len(sections),
                "pages_unreadable": failed_pages,
                "truncated": len(reader.pages) > MAX_PAGES,
            },
        )

