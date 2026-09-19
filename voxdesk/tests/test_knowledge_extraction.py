"""
Extraction and file validation.

These use real bytes -- a genuine PDF, a genuine DOCX built by python-docx --
because the interesting failures live in the parsers, not in our wrapper. A
mocked extractor would pass every one of these tests while the real thing
returned an empty string.
"""
from __future__ import annotations

import json

import pytest

from app.knowledge.extractors import (
    ExtractionError,
    clean_text,
    detect_format,
    extract,
)
from tests.knowledge_fixtures import make_docx, make_pdf


# ------------------------------------------------------------------ text ---

def test_plain_text_extracts():
    result = extract(b"We open at nine and close at five.", filename="hours.txt")
    assert "open at nine" in result.text
    assert result.metadata["format"] == "txt"


def test_markdown_headings_become_sections():
    source = b"# Refunds\n\nFourteen days.\n\n# Parking\n\nFree lot out back.\n"
    result = extract(source, filename="faq.md")
    headings = [s.metadata.get("heading") for s in result.sections]
    assert headings == ["Refunds", "Parking"]


def test_utf16_text_is_decoded():
    """Windows exports are UTF-16, and a mis-decode looks like binary garbage."""
    result = extract("Café hours: nine to five".encode("utf-16"), filename="h.txt")
    assert "Caf" in result.text and "nine to five" in result.text


def test_control_characters_are_stripped():
    dirty = "Price is\x00 120\x07 dollars\x1b".encode()
    result = extract(dirty, filename="p.txt")
    assert "\x00" not in result.text
    assert "120" in result.text


def test_clean_text_normalizes_whitespace_and_ligatures():
    assert clean_text("a\r\nb\n\n\n\nc   d  \n") == "a\nb\n\nc d"
    # NFKC folds the ligature, so the same word embeds identically whichever
    # tool produced the file.
    assert clean_text("of\ufb01ce") == "office"


def test_bidi_override_characters_are_removed():
    """
    Direction overrides render one way and read another -- a way to hide an
    instruction inside otherwise innocent text.
    """
    assert "\u202e" not in clean_text("safe\u202etxet neddih")


# ------------------------------------------------------------------- pdf ---

def test_pdf_extracts_every_page_with_its_number():
    data = make_pdf([
        "Refund Policy. Refunds within fourteen days.",
        "Pricing. A cleaning costs 120 dollars.",
        "Parking. Free lot behind the building.",
    ])
    result = extract(data, filename="policy.pdf")

    assert result.metadata["format"] == "pdf"
    assert result.metadata["page_count"] == 3
    assert [s.metadata["page"] for s in result.sections] == [1, 2, 3]
    assert "120 dollars" in result.sections[1].text


def test_malformed_pdf_fails_with_a_useful_message_not_a_crash():
    with pytest.raises(ExtractionError) as exc:
        extract(b"%PDF-1.4\n" + b"garbage" * 40, filename="broken.pdf")
    assert "PDF" in str(exc.value)


def test_pdf_with_no_extractable_text_is_reported_as_needing_ocr():
    # A page whose content stream draws nothing produces no text -- the same
    # shape as a scanned document.
    with pytest.raises(ExtractionError) as exc:
        extract(make_pdf([""]), filename="scan.pdf")
    assert "scan" in str(exc.value).lower() or "OCR" in str(exc.value)


# ------------------------------------------------------------------ docx ---

def test_docx_preserves_paragraph_order_and_headings():
    data = make_docx([
        ("Heading 1", "Refund Policy"),
        ("Normal", "Refunds are issued within fourteen days."),
        ("Heading 1", "Hours"),
        ("Normal", "Open nine to five."),
    ])
    result = extract(data, filename="policy.docx")

    headings = [s.metadata.get("heading") for s in result.sections]
    assert headings == ["Refund Policy", "Hours"]
    # Order within the document is the reading order.
    assert result.text.index("fourteen days") < result.text.index("nine to five")


def test_corrupt_docx_fails_cleanly():
    with pytest.raises(ExtractionError):
        extract(b"PK\x03\x04" + b"\x00" * 100, filename="broken.docx")


# ------------------------------------------------------- csv and json ------

def test_csv_becomes_labelled_prose_keeping_field_names():
    """
    Raw CSV embeds badly. Every value must stay attached to its column name so
    "how much is a cleaning" can match the price row.
    """
    data = b"service,price,duration\nCleaning,120 dollars,45 minutes\n"
    result = extract(data, filename="prices.csv")

    assert "service: Cleaning" in result.text
    assert "price: 120 dollars" in result.text
    assert "," not in result.text.split("\n")[0].replace("120", "")


def test_csv_with_semicolon_delimiter_is_sniffed():
    result = extract(b"item;cost\nWidget;9\n", filename="p.csv")
    assert "item: Widget" in result.text and "cost: 9" in result.text


def test_empty_csv_fails():
    with pytest.raises(ExtractionError):
        extract(b"\n\n\n", filename="empty.csv")


def test_json_is_flattened_to_dotted_paths_not_dumped_as_syntax():
    data = json.dumps(
        {"pricing": {"cleaning": 120}, "hours": {"monday": "9-5"}}
    ).encode()
    result = extract(data, filename="kb.json")

    assert "pricing.cleaning: 120" in result.text
    assert "hours.monday: 9-5" in result.text
    # No JSON punctuation should reach a prompt.
    assert "{" not in result.text and '"' not in result.text


def test_json_list_of_scalars_reads_naturally():
    data = json.dumps({"services": ["cleaning", "whitening"]}).encode()
    assert "services: cleaning, whitening" in extract(data, filename="s.json").text


def test_malformed_json_fails_safely():
    with pytest.raises(ExtractionError) as exc:
        extract(b'{"unclosed": ', filename="bad.json")
    assert "JSON" in str(exc.value)


# ------------------------------------------------------------ validation ---

@pytest.mark.parametrize(
    "data,filename",
    [
        (b"MZ\x90\x00\x03\x00\x00\x00", "invoice.pdf"),   # PE renamed to .pdf
        (b"\x7fELF\x02\x01\x01\x00", "notes.txt"),        # ELF renamed to .txt
        (b"#!/bin/bash\nrm -rf /", "readme.txt"),          # script renamed
    ],
)
def test_executables_are_rejected_however_they_are_named(data, filename):
    """The client's filename and MIME type are hints; the bytes are the truth."""
    with pytest.raises(ExtractionError):
        detect_format(data, filename=filename, declared_mime="text/plain")


def test_archives_are_rejected():
    with pytest.raises(ExtractionError) as exc:
        detect_format(b"PK\x03\x04" + b"\x00" * 40, filename="docs.zip")
    assert "archive" in str(exc.value).lower()


@pytest.mark.parametrize(
    "extension", [".exe", ".sh", ".js", ".php", ".docm", ".html", ".svg"]
)
def test_dangerous_extensions_are_refused(extension):
    with pytest.raises(ExtractionError):
        detect_format(b"harmless looking text", filename=f"file{extension}")


def test_lying_content_type_cannot_smuggle_a_format():
    """A caller claiming application/pdf does not make a .exe a PDF."""
    with pytest.raises(ExtractionError):
        detect_format(
            b"MZ\x90\x00", filename="x.exe", declared_mime="application/pdf"
        )


def test_pdf_extension_with_non_pdf_content_is_a_mismatch():
    with pytest.raises(ExtractionError) as exc:
        detect_format(b"just some text, not a pdf at all", filename="report.pdf")
    assert "not a valid" in str(exc.value).lower()


def test_docx_bytes_are_detected_even_when_misnamed():
    """Magic wins in the other direction too -- a real DOCX still extracts."""
    data = make_docx([("Normal", "Open nine to five.")])
    assert detect_format(data, filename="policy.pdf") == "docx"


def test_unsupported_type_is_rejected_with_the_accepted_list():
    with pytest.raises(ExtractionError) as exc:
        detect_format(b"plain content", filename="drawing.dwg")
    assert "PDF" in str(exc.value) and "CSV" in str(exc.value)


def test_empty_file_is_rejected():
    with pytest.raises(ExtractionError):
        extract(b"", filename="empty.txt")