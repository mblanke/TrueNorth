"""The unit readiness page as a PDF, with the AAR's machinery (fpdf2 core fonts and
``aar_html.pdf_text``, which transliterates what latin-1 cannot draw). Landscape A4: a
summary, the qualifications earned, then each class and its Students. Pass/fail only."""

from __future__ import annotations

from io import BytesIO

from fpdf import FPDF

from ..aar_html import pdf_text as _s
from .readiness_schemas import UnitReadinessOut

_COLUMNS = ("Student", "Unit", "Enrolment", "Present", "Excused", "Absent", "Result", "Comment", "Qualification")
_WIDTHS = (46, 34, 24, 16, 16, 16, 20, 74, 24)


def render(report: UnitReadinessOut, *, compress: bool = True) -> bytes:
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_compression(compress)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    def line(text: str, size: int = 10, style: str = "", h: float = 6) -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(0, h, _s(text), new_x="LMARGIN", new_y="NEXT")

    s = report.summary
    line("Unit readiness", 18, "B", 10)
    line(f"{report.date_from.isoformat()} to {report.date_to.isoformat()}   Unit: {report.unit or 'all units'}")
    pdf.ln(2)
    line(f"Classes {s.classes}   Places {s.enrolments}   Students {s.students}   Sessions held {s.sessions_held}", 11, "B")
    line(f"Attendance: present {s.present}, excused {s.excused}, absent {s.absent}")
    line(f"Results: pass {s.passed}, fail {s.failed}, incomplete {s.incomplete}, awaiting {s.awaiting_result}")
    line(f"Qualifications earned: {s.qualifications_earned}"
         + "".join(f"   {q.code} ({q.title}): {q.earned}" for q in report.qualifications))
    for c in report.classes:
        pdf.ln(4)
        qual = f", delivers {c.qualification}" if c.qualification else ""
        line(f"{c.name}: {c.course_name}{qual}", 12, "B", 7)
        line(f"{c.start_date.isoformat()} to {c.end_date.isoformat()}, {c.status}, {c.sessions_held} sessions held, "
             f"{c.enrolled} Students: pass {c.passed}, fail {c.failed}, incomplete {c.incomplete}, "
             f"awaiting {c.awaiting_result}", 9)
        if not c.students:
            continue
        pdf.set_font("Helvetica", "", 8)
        with pdf.table(col_widths=_WIDTHS, text_align="LEFT", line_height=4.5, first_row_as_headings=True) as table:
            table.row([_s(h) for h in _COLUMNS])
            for st in c.students:
                table.row([_s(v) for v in (
                    st.display_name or st.email, st.unit or "", st.enrolment_status or "", st.present, st.excused,
                    st.absent, st.outcome.value if st.outcome else "-", st.comment or "",
                    st.qualification_earned or "")])
    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
