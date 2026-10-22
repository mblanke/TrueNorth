"""The certification record as a PDF, with the AAR's machinery (fpdf2 core fonts and
``aar_html.pdf_text``). The PDF is a readable copy, not the evidence: it prints the
record's ``record_sha256``, which every signed JSON export of the record in this state
carries too (it does not depend on who exported it or when), so a PDF can be matched to a
JSON export that verifies."""

from __future__ import annotations

import json
from io import BytesIO

from fpdf import FPDF

from ..aar_html import pdf_text as _s

_MARK = {"pass": "[PASS]", "fail": "[FAIL]", "incomplete": "[INCOMPLETE]", "missing": "[MISSING]", "info": "[INFO]"}


def render(signed: dict, *, compress: bool = True) -> bytes:
    rec, sig = signed["record"], signed["signature"]
    pdf = FPDF()
    pdf.set_compression(compress)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(text: str, size: int = 10, style: str = "", h: float = 6) -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(0, h, _s(text), new_x="LMARGIN", new_y="NEXT")

    line("Range Certification Record", 18, "B", 10)
    line(f"Verdict: {rec['verdict_label']}", 14, "B", 8)
    for reason in rec["reasons"]:
        line(f"  - {reason}")
    pdf.ln(2)
    line(f"Range: {rec['range_name']} ({rec['range_id']})")
    line(f"Deployment: {rec['deployment_id']} (generation {rec['generation']})")
    line(f"Template: {rec['template_name']} {rec['template_version']}")
    line(f"Plan hash ({rec['plan_hash_basis']}): {rec['plan_hash']}")
    line(f"Opened {rec['opened_at']} by {rec['opened_by']}")
    line(f"Exported {signed['exported_at']} by {signed['exported_by']}")
    pdf.ln(3)
    for sec in rec["sections"]:
        line(f"{_MARK.get(sec['status'], sec['status'])} {sec['title']}"
             f"{'' if sec['required'] else ' (optional)'} - from {sec['provided_by']}", 12, "B", 7)
        if sec["summary"]:
            line(sec["summary"])
        for e in sec["entries"]:
            who = e["actor"] + (f", reason: {e['reason']}" if e.get("reason") else "")
            fix = f", corrects #{e['corrects_seq']}" if e.get("corrects_seq") else ""
            line(f"  #{e['seq']} {e['recorded_at']} {e['status']} ({e['source']}; {who}{fix}): {e['summary']}", 8)
        pdf.ln(2)
    pdf.ln(2)
    line("Signature", 12, "B", 7)
    line(f"Record SHA-256: {signed['record_sha256']}", 9)
    line(f"Signed by {sig['alg']} key {sig['key_id']}. Verify a JSON export of this record "
         "(GET /ranges/{id}/certification/export) whose record_sha256 matches; the PDF itself is not signed.", 9)
    meta = json.dumps({"record": rec["id"], "record_sha256": signed["record_sha256"]})
    pdf.set_subject(_s(meta))
    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
