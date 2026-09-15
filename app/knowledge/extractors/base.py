"""
Text extraction interface.

Every extractor turns raw bytes into normalized text plus whatever structural
metadata it could recover -- page numbers for PDFs, heading trails for DOCX,
field names for CSV/JSON. Chunking later uses that metadata so a retrieved
passage can say where it came from.

Two rules apply to every extractor:

1. It must never execute the document. No macros, no external entity
   resolution, no shelling out to a converter.
2. It must not crash the ingestion worker. A malformed file is a normal
   business event, so extractors raise `ExtractionError`, which the pipeline
   records as a FAILED document with a safe message.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

#: Control characters that survive a bad PDF or a mis-decoded byte stream.
#: Tab, newline and carriage return are kept; everything else in C0/C1 goes.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_MANY_SPACES = re.compile(r"[ \t]{2,}")
#: Unicode direction overrides. They render one way and read another, which is
#: exactly what someone hiding an instruction in a document would want.
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


class ExtractionError(Exception):
    """The document could not be read. Message is safe to show a tenant."""


@dataclass
class Section:
    """A page, sheet, or logical block, with whatever label the format gave."""

    text: str
    #: e.g. {"page": 3} or {"heading": "Refund policy"} or {"row_range": "1-50"}
    metadata: dict = field(default_factory=dict)


@dataclass
class ExtractedDocument:
    text: str
    sections: list[Section] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def clean_text(raw: str) -> str:
    """
    Normalize extracted text.

    NFKC folds the lookalike characters ("ﬁ" -> "fi", full-width Latin -> ASCII)
    that would otherwise make the same sentence embed differently depending on
    which tool produced the file.
    """
    if not raw:
        return ""

    text = unicodedata.normalize("NFKC", raw)
    text = _BIDI.sub("", text)
    text = _CONTROL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _MANY_SPACES.sub(" ", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _MANY_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    """
    Rough token count.

    Deliberately not a real tokenizer: every provider tokenizes differently,
    and pulling in tiktoken to size a chunk would tie chunking to one vendor.
    Four characters per token is the usual English approximation and is only
    ever used for budgeting, never for correctness.
    """
    return max(1, len(text) // 4) if text else 0


class Extractor(Protocol):
    def extract(self, data: bytes, *, filename: str | None = None) -> ExtractedDocument:
        ...