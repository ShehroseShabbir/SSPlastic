# core/utils_pdf.py
from __future__ import annotations
from pathlib import Path
from decimal import Decimal
from datetime import date, datetime
from calendar import monthrange

from django.conf import settings
from django.utils import timezone
from django.db.models.functions import Coalesce
from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from xml.sax.saxutils import escape

# money utils from your project
from .utils_money import D, to_rupees_int
from .utils_weight import dkg

# ---------- Formatting ----------
def pkr_str(val) -> str:
    return f"Rs. {to_rupees_int(D(val)):,}"

def _as_lines(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).splitlines() if s.strip()]

# Paragraph style used in cells (no zero-width chars → no tofu squares)
PSTYLE_NOZWSP = ParagraphStyle(
    "nozwsp", fontName="Helvetica", fontSize=9, leading=11,
    wordWrap="CJK", spaceBefore=0, spaceAfter=0,
)

def p_wrap(text) -> Paragraph:
    """Wrap text for tables without injecting zero-width characters."""
    s = "" if text == "" else (str(text) if text is not None else "—")
    return Paragraph(escape(s), PSTYLE_NOZWSP)

def cell_text(cell: object) -> str:
    """Flatten Paragraphs and None safely; keep empty strings as '' (not '—')."""
    if cell == "":
        return ""
    if hasattr(cell, "getPlainText"):
        try:
            return cell.getPlainText()
        except Exception:
            pass
    if cell is None:
        return "—"
    return str(cell)

# ---------- Auto column widths ----------
def auto_col_widths(rows, avail_width_pt: float, *, font="Helvetica", size=9, pad=6, min_pt=42):
    if not rows:
        return []
    ncols = len(rows[0])
    widest = [0.0] * ncols
    for r in rows:
        for j, cell in enumerate(r):
            s = cell_text(cell)
            w = stringWidth(s, font, size) + 2 * pad
            if w > widest[j]:
                widest[j] = w
    widest = [max(min_pt, w) for w in widest]
    total = sum(widest)
    if total <= 0:
        return [avail_width_pt / ncols] * ncols
    scale = min(1.0, avail_width_pt / total)
    widths = [w * scale for w in widest]
    # distribute any rounding slack
    diff = avail_width_pt - sum(widths)
    if abs(diff) > 0.01:
        bump = diff / ncols
        widths = [w + bump for w in widths]
    return widths

def build_auto_table(
    data,
    *,
    text_cols: set[int],
    num_cols: set[int],
    avail_width: float,
    repeat_header: bool = True,
    base_style: TableStyle | None = None,
):
    """Auto-fit table with wrapping on text columns and right-aligned numeric columns."""
    # Wrap text columns only for DATA rows (header stays plain)
    wrapped = []
    for i, row in enumerate(data):
        if i == 0:
            wrapped.append([cell_text(c) for c in row])
            continue
        new_row = []
        for j, cell in enumerate(row):
            new_row.append(p_wrap(cell) if j in text_cols else cell_text(cell))
        wrapped.append(new_row)

    # Measure with plain strings
    measure_rows = [[cell_text(c) for c in row] for row in data]
    col_widths = auto_col_widths(measure_rows, avail_width_pt=avail_width)

    t = Table(wrapped, colWidths=col_widths, repeatRows=(1 if repeat_header else 0))
    t_style = TableStyle(base_style.getCommands() if base_style else [])
    t_style.add("VALIGN", (0, 0), (-1, -1), "TOP")
    t_style.add("LEFTPADDING",  (0, 0), (-1, -1), 4)
    t_style.add("RIGHTPADDING", (0, 0), (-1, -1), 4)
    t_style.add("ALIGN", (0, 1), (-1, -1), "LEFT")
    if num_cols:
        t_style.add("ALIGN", (min(num_cols), 1), (max(num_cols), -1), "RIGHT")
    t.setStyle(t_style)

    tw, th = t.wrapOn(None, avail_width, 0)
    return t, tw, th

# ---------- Page helpers ----------
def draw_multiline_left(c, lines, x, y, *, leading=12, font="Helvetica", size=9):
    c.setFont(font, size)
    cur = y
    for ln in _as_lines(lines):
        c.drawString(x, cur, ln); cur -= leading
    return cur

def draw_multiline_right(c, lines, x_right, y, *, leading=12, font="Helvetica", size=9):
    c.setFont(font, size)
    cur = y
    for ln in _as_lines(lines):
        c.drawRightString(x_right, cur, ln); cur -= leading
    return cur

# ---------- Themes & period bounds ----------
def get_billing_theme(ss, default="default"):
    try:
        t = (getattr(ss, "billing_theme", None) or "").strip().lower()
    except Exception:
        t = ""
    return t or default

def period_bounds(year: int, month: int):
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    tz = timezone.get_current_timezone()
    return (
        first,
        last,
        datetime.combine(first, datetime.min.time(), tzinfo=tz),
        datetime.combine(last,  datetime.max.time(), tzinfo=tz),
    )

def period_bounds_from_dates(start_date: date, end_date: date):
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    tz = timezone.get_current_timezone()
    return (
        start_date,
        end_date,
        datetime.combine(start_date, datetime.min.time(), tzinfo=tz),
        datetime.combine(end_date,   datetime.max.time(), tzinfo=tz),
    )

def statement_path(customer_name: str, *, year: int | None = None, month: int | None = None,
                   start: date | None = None, end: date | None = None) -> Path:
    base = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    base.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in customer_name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    if year is not None and month is not None:
        return base / f"Statement-{safe}-{year:04d}-{month:02d}.pdf"
    return base / f"Statement-{safe}-{start.isoformat()}_to_{end.isoformat()}.pdf"

def nice_range_label(start, end):
    def _fmt(d):
        if isinstance(d, str): return d
        if isinstance(d, datetime): d = d.date()
        if isinstance(d, date): return d.strftime("%d %b %Y")
        return str(d)
    return f"{_fmt(start)} → {_fmt(end)}"

BASE_TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -2), 0.5, colors.black),
    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("BACKGROUND", (0, -1), (-1, -1), colors.black),
    ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
])