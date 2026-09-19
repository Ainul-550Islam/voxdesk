"""
Deterministic document fixtures.

Real PDF/DOCX bytes are built in memory rather than committed as binaries, so
the test suite stays reviewable in a diff and cannot drift from the library
versions that actually parse them.
"""
from __future__ import annotations

import io


def make_pdf(pages: list[str]) -> bytes:
    """
    Build a minimal but genuine multi-page PDF.

    Written by hand instead of with a PDF library because the only production
    dependency is pypdf, which reads but does not write formatted text. The
    output is a valid PDF 1.4 with one text object per page.
    """
    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    objects: list[bytes] = []
    page_count = len(pages)
    # 1 = catalog, 2 = page tree, 3 = font, then (page, content) per page.
    page_ids = [4 + i * 2 for i in range(page_count)]

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids).encode()
    objects.append(
        b"<< /Type /Pages /Kids [" + kids + b"] /Count "
        + str(page_count).encode() + b" >>"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, body in enumerate(pages):
        content_id = page_ids[index] + 1
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + str(content_id).encode() + b" 0 R >>"
        )
        lines = body.split("\n")
        stream = b"BT /F1 12 Tf 72 720 Td 14 TL\n"
        for line in lines:
            stream += b"(" + escape(line).encode("latin-1", "replace") + b") Tj T*\n"
        stream += b"ET"
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


def make_docx(blocks: list[tuple[str, str]]) -> bytes:
    """
    Build a DOCX from (style, text) pairs, e.g. ("Heading 1", "Refund policy")
    or ("Normal", "Refunds take 14 days.").
    """
    import docx

    document = docx.Document()
    for style, text in blocks:
        if style.startswith("Heading"):
            document.add_heading(text, level=int(style.split()[-1]))
        else:
            document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()