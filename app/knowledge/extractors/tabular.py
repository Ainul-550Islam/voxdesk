"""
CSV and JSON -> retrieval-friendly prose.

Dumping raw syntax into a prompt wastes context on punctuation and embeds
badly: `{"price": 120}` and "the price is 120 dollars" are far apart in vector
space even though a caller asking about price wants the first one.

So structured data is rendered as labelled statements that keep every field
name:

    CSV row  ->  "service: Cleaning | price: $120 | duration: 45 min"
    JSON     ->  "pricing.cleaning.price: 120"

Field names survive, the retrieval query "how much is a cleaning" matches, and
nothing about the original data is lost.
"""
from __future__ import annotations

import csv
import io
import json

from app.knowledge.extractors.base import (
    ExtractedDocument,
    ExtractionError,
    Section,
    clean_text,
)
from app.knowledge.extractors.text import _decode

#: Rows per section. Keeps a wide spreadsheet from becoming one giant chunk.
CSV_ROWS_PER_SECTION = 40
MAX_CSV_ROWS = 20_000
#: How deep to walk a nested JSON structure before flattening to a string.
MAX_JSON_DEPTH = 12


class CsvExtractor:
    def extract(self, data: bytes, *, filename: str | None = None) -> ExtractedDocument:
        if not data:
            raise ExtractionError("the file is empty")

        text = _decode(data)
        try:
            # Sniffing handles semicolon-separated exports from European Excel.
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        try:
            reader = csv.reader(io.StringIO(text), dialect)
            rows = [row for _, row in zip(range(MAX_CSV_ROWS), reader)]
        except csv.Error as exc:
            raise ExtractionError("this CSV file is malformed") from exc

        rows = [r for r in rows if any((cell or "").strip() for cell in r)]
        if not rows:
            raise ExtractionError("this CSV file has no rows")

        header = [(h or "").strip() or f"column_{i + 1}" for i, h in enumerate(rows[0])]
        body = rows[1:] or rows        # a header-only file is still content

        lines: list[str] = []
        for row in body:
            pairs = [
                f"{header[i] if i < len(header) else f'column_{i + 1}'}: {value.strip()}"
                for i, value in enumerate(row)
                if (value or "").strip()
            ]
            if pairs:
                lines.append(" | ".join(pairs))

        if not lines:
            raise ExtractionError("this CSV file has no usable values")

        sections = [
            Section(
                text=clean_text("\n".join(lines[start:start + CSV_ROWS_PER_SECTION])),
                metadata={
                    "row_start": start + 1,
                    "row_end": min(start + CSV_ROWS_PER_SECTION, len(lines)),
                    "columns": header,
                },
            )
            for start in range(0, len(lines), CSV_ROWS_PER_SECTION)
        ]

        return ExtractedDocument(
            text="\n".join(s.text for s in sections),
            sections=sections,
            metadata={"format": "csv", "row_count": len(lines), "columns": header},
        )


def _flatten(value, prefix: str = "", depth: int = 0) -> list[str]:
    """Recursively render JSON as `dotted.path: value` lines."""
    if depth > MAX_JSON_DEPTH:
        return [f"{prefix}: {value!s:.200}"]

    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            lines.extend(_flatten(item, child, depth + 1))
        return lines

    if isinstance(value, list):
        # Lists of scalars read better joined than as `field.0`, `field.1`.
        if all(not isinstance(v, (dict, list)) for v in value):
            rendered = ", ".join("" if v is None else str(v) for v in value)
            return [f"{prefix}: {rendered}"] if rendered else []
        lines = []
        for index, item in enumerate(value):
            lines.extend(_flatten(item, f"{prefix}[{index}]", depth + 1))
        return lines

    if value is None or value == "":
        return []
    return [f"{prefix}: {value}"]


class JsonExtractor:
    def extract(self, data: bytes, *, filename: str | None = None) -> ExtractedDocument:
        if not data:
            raise ExtractionError("the file is empty")

        try:
            parsed = json.loads(_decode(data))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExtractionError("this file is not valid JSON") from exc

        lines = _flatten(parsed)
        if not lines:
            raise ExtractionError("this JSON file contains no readable values")

        # Top-level keys make natural section boundaries.
        sections: list[Section] = []
        if isinstance(parsed, dict) and len(parsed) > 1:
            for key, value in parsed.items():
                body = clean_text("\n".join(_flatten(value, str(key))))
                if body:
                    sections.append(Section(text=body, metadata={"heading": str(key)}))
        if not sections:
            sections = [Section(text=clean_text("\n".join(lines)), metadata={})]

        return ExtractedDocument(
            text="\n".join(s.text for s in sections),
            sections=sections,
            metadata={"format": "json", "field_count": len(lines)},
        )

