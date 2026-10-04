"""PDF text extraction for curriculum uploads.

`_extract_pdf` is the only pypdf call site. It reads untrusted uploads, so pypdf is
kept current for security fixes. These tests pin the behaviour across those upgrades:
every page's text comes through, page order is kept, and a malformed file raises
instead of returning an empty string that would be indexed as a blank course.
"""

from __future__ import annotations

import pytest
from fpdf import FPDF


def _pdf(*pages: str) -> bytes:
    doc = FPDF()
    doc.set_font("Helvetica", size=12)
    for text in pages:
        doc.add_page()
        doc.cell(text=text)
    return bytes(doc.output())


def test_extracts_every_page_in_order():
    from app.curriculum_ingest import extract_text

    text = extract_text("lesson.pdf", _pdf("Enabling objective one", "Enabling objective two"))

    first = text.index("Enabling objective one")
    second = text.index("Enabling objective two")
    assert first < second
    assert "\n\n" in text[first:second]


def test_dispatches_on_mime_type_without_extension():
    from app.curriculum_ingest import extract_text

    text = extract_text("upload", _pdf("Range safety brief"), mime_type="application/pdf")

    assert "Range safety brief" in text


def test_malformed_pdf_raises():
    from app.curriculum_ingest import extract_text
    from pypdf.errors import PdfReadError

    with pytest.raises(PdfReadError):
        extract_text("broken.pdf", b"%PDF-1.7\nnot really a pdf")
