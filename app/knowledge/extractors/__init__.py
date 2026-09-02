"""
Format detection and extractor dispatch.

The client-supplied MIME type is treated as a hint, never as truth: a browser
will happily send `application/pdf` for a shell script. Selection is therefore
driven by the file's own magic bytes first, then the extension, and the two
must agree on a format we support.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.extractors.base import (
    ExtractedDocument,
    ExtractionError,
    Extractor,
    Section,
    clean_text,
    estimate_tokens,
)
from app.knowledge.extractors.docx import DocxExtractor
from app.knowledge.extractors.pdf import PdfExtractor
from app.knowledge.extractors.tabular import CsvExtractor, JsonExtractor
from app.knowledge.extractors.text import TextExtractor

__all__ = [
    "ExtractedDocument", "ExtractionError", "Extractor", "Section",
    "clean_text", "estimate_tokens",
    "DocumentFormat", "SUPPORTED_FORMATS", "detect_format", "get_extractor",
    "extract",
]


@dataclass(frozen=True)
class DocumentFormat:
    name: str
    extensions: tuple[str, ...]
    mime_types: tuple[str, ...]


SUPPORTED_FORMATS: dict[str, DocumentFormat] = {
    "pdf": DocumentFormat("pdf", (".pdf",), ("application/pdf",)),
    "docx": DocumentFormat(
        "docx", (".docx",),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    ),
    "txt": DocumentFormat("txt", (".txt",), ("text/plain",)),
    "md": DocumentFormat("md", (".md", ".markdown"), ("text/markdown", "text/x-markdown")),
    "csv": DocumentFormat("csv", (".csv",), ("text/csv", "application/csv")),
    "json": DocumentFormat("json", (".json",), ("application/json", "text/json")),
}

_EXTRACTORS: dict[str, Extractor] = {
    "pdf": PdfExtractor(),
    "docx": DocxExtractor(),
    "txt": TextExtractor(),
    "md": TextExtractor(),
    "csv": CsvExtractor(),
    "json": JsonExtractor(),
}

#: Formats we explicitly refuse, with a reason worth showing the user. Anything
#: not in SUPPORTED_FORMATS is rejected regardless; these get a better message.
DANGEROUS_EXTENSIONS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".com", ".msi",
    ".sh", ".bash", ".zsh", ".ps1", ".py", ".rb", ".pl", ".php",
    ".js", ".mjs", ".jar", ".apk", ".scr", ".vbs", ".wsf", ".lnk",
    ".doc", ".xls", ".ppt",          # legacy formats carry macros
    ".docm", ".xlsm", ".pptm",
    ".html", ".htm", ".svg", ".xml",  # active content / entity expansion
    ".zip", ".tar", ".gz", ".7z", ".rar",
})


def _extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].strip().lower()


def sniff_magic(data: bytes) -> str | None:
    """
    Identify a format from its leading bytes. Returns None when unrecognised
    (which is normal and fine for plain text).
    """
    if not data:
        return None
    head = data[:8]

    if head.startswith(b"%PDF-"):
        return "pdf"
    # DOCX is a zip. Distinguishing it from any other zip needs a look inside.
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "docx" if _is_docx_zip(data) else "zip"
    # Formats we must never accept, caught even when renamed to .txt.
    if head.startswith(b"MZ"):
        return "executable"
    if head.startswith(b"\x7fELF"):
        return "executable"
    if head.startswith(b"#!"):
        return "script"
    if head.startswith(b"\xca\xfe\xba\xbe"):
        return "executable"
    return None


#: UTF-16 and UTF-8 byte-order marks. A BOM is a positive declaration that the
#: file is text, which matters because UTF-16 is roughly half NUL bytes and
#: would otherwise trip every binary heuristic ever written.
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")


def _looks_binary(data: bytes) -> bool:
    """
    Whether these bytes are a binary blob rather than a text document.

    A single stray NUL is a mis-encoded export, not an executable, and
    rejecting the whole price list over it would be obnoxious -- the cleaner
    strips it. So the test is proportional: text is judged binary only when
    control bytes make up a real share of the sample.
    """
    if data.startswith(_BOMS):
        return False

    sample = data[:4096]
    if not sample:
        return False

    # Everything in C0 except tab, newline, carriage return and form feed.
    control = sum(
        1 for byte in sample if byte < 0x09 or 0x0e <= byte < 0x20 or byte == 0x7f
    )
    # Both conditions must hold. The ratio alone would condemn a short note
    # containing three stray control bytes; the count alone would let a large
    # binary through on a technicality.
    return control > 8 and control / len(sample) > 0.01


def _is_docx_zip(data: bytes) -> bool:
    """A DOCX always contains word/document.xml."""
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except Exception:
        return False
    return "word/document.xml" in names


def detect_format(
    data: bytes, *, filename: str | None = None, declared_mime: str | None = None
) -> str:
    """
    Decide which extractor to use, or raise `ExtractionError`.

    Magic bytes win. The declared MIME type is only consulted when the content
    is not self-identifying, and even then it must name a supported format.
    """
    extension = _extension(filename)
    magic = sniff_magic(data)

    if magic in ("executable", "script"):
        raise ExtractionError("executable files are not accepted")
    if magic == "zip":
        raise ExtractionError("archives are not accepted; upload the documents inside")

    if magic == "pdf":
        return "pdf"
    if magic == "docx":
        return "docx"

    if _looks_binary(data):
        raise ExtractionError("this file does not appear to be a readable document")

    if extension in DANGEROUS_EXTENSIONS:
        raise ExtractionError(f"{extension} files are not accepted")

    for name, fmt in SUPPORTED_FORMATS.items():
        if extension in fmt.extensions:
            # A .pdf whose bytes are not a PDF is a mismatch, not a PDF.
            if name in ("pdf", "docx"):
                raise ExtractionError(
                    f"the file is named {extension} but its contents are not a "
                    f"valid {name.upper()}"
                )
            return name

    declared = (declared_mime or "").split(";")[0].strip().lower()
    for name, fmt in SUPPORTED_FORMATS.items():
        if declared in fmt.mime_types and name not in ("pdf", "docx"):
            return name

    raise ExtractionError(
        "unsupported file type -- accepted formats are "
        "PDF, DOCX, TXT, Markdown, CSV and JSON"
    )


def get_extractor(format_name: str) -> Extractor:
    extractor = _EXTRACTORS.get(format_name)
    if extractor is None:
        raise ExtractionError(f"no extractor for {format_name}")
    return extractor


def extract(
    data: bytes, *, filename: str | None = None, declared_mime: str | None = None
) -> ExtractedDocument:
    """Detect the format and extract. Raises `ExtractionError` on any failure."""
    format_name = detect_format(data, filename=filename, declared_mime=declared_mime)
    document = get_extractor(format_name).extract(data, filename=filename)
    # The registry name is authoritative, so metadata["format"] always matches
    # the `mime_type` column the document row records. An extractor's own,
    # finer-grained label (markdown vs plain text) moves aside rather than
    # competing with it.
    inner = document.metadata.get("format")
    if inner and inner != format_name:
        document.metadata["text_format"] = inner
    document.metadata["format"] = format_name
    return document

