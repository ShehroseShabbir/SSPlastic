# core/services/purchase_pdf.py
from decimal import Decimal as D
from pathlib import Path
from calendar import monthrange
from django.conf import settings
from django.utils import timezone
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Table, TableStyle
from reportlab.pdfgen import canvas

from core.models.raw_material import RawMaterialTxn
try:
    # re-use your helpers if present
    from core.utils import _draw_page_header, _draw_footer  # adjust if you keep them elsewhere
except Exception:
    def _draw_page_header(c, W, H, ss):  # minimal fallback
        c.setFont("Helvetica-Bold", 11)
        c.drawString(15*mm, H - 10*mm, getattr(ss, "company_name", getattr(settings, "COMPANY_NAME", "Your Company")))

    def _draw_footer(c, W, H, ss, *, payment_terms=None, bank_lines=None, ref=None, user=None):
        c.setFont("Helvetica-Oblique", 8)
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        c.drawRightString(W - 15*mm, 10*mm, f"Generated {stamp}" + (f" · {ref}" if ref else ""))

try:
    from core.utils import pkr_str
except Exception:
    def pkr_str(val):  # PKR ints/decimals -> "Rs. 12,345"
        try:
            n = int(D(val).quantize(D("1")))
        except Exception:
            n = int(val or 0)
        return f"Rs. {n:,}"
def _as_lines(v):
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).splitlines() if s.strip()]

def _draw_multiline_right(c, lines, right_x, top_y, *, leading=14, font="Helvetica", size=10):
    c.setFont(font, size)
    y = top_y
    for line in lines:
        c.drawRightString(right_x, y, str(line))
        y -= leading
    return y

def _safe_lines(v):
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).splitlines() if s.strip()]

TABLE_STYLE = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
])

def _purchase_pdf_path(purchase: RawMaterialTxn) -> Path:
    base = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    base.mkdir(parents=True, exist_ok=True)
    safe_supplier = "".join(ch for ch in (purchase.supplier_name or "Supplier") if ch.isalnum() or ch in (" ", "_", "-")).strip() or "Supplier"
    return base / f"RM-Purchase-{safe_supplier}-{purchase.when:%Y-%m-%d}-#{purchase.pk}.pdf"

def _get_bank_lines(ss):
    # adapt to your SiteSettings shape
    if not ss:
        return []
    bd = getattr(ss, "bank_details_list", None)
    if bd is not None:
        return bd() if callable(bd) else list(bd)
    return _safe_lines(getattr(ss, "bank_details", ""))

def _collect_purchase_payments(purchase):
    """
    Returns (rows, total_int) for the payments table.
    rows: [[date, method, bank/ref, "Rs. …"], ...]
    total_int: integer rupees sum of linked payments.
    """
    links = purchase.linked_payments.select_related("payment").order_by(
        "payment__paid_on", "payment_id"
    )

    rows, total_int = [], 0
    for link in links:
        pay = link.payment
        if not pay:
            continue
        # amount from Payment (Decimal) → whole-rupees int
        try:
            amt_int = int(D(str(pay.amount_pkr or 0)).quantize(D("1")))
        except Exception:
            amt_int = 0

        total_int += amt_int

        ref_bits = []
        bank = getattr(pay, "bank", "") or ""
        if bank:
            ref_bits.append(str(bank))
        ref = getattr(pay, "reference", "") or ""
        if ref:
            ref_bits.append(str(ref))
        ref_txt = " / ".join(ref_bits) or "—"

        rows.append([
            str(getattr(pay, "paid_on", "") or "—"),
            getattr(pay, "get_method_display", lambda: getattr(pay, "method", "—"))() or "—",
            ref_txt,
            pkr_str(amt_int),
        ])

    return rows, int(total_int)

def generate_rm_purchase_statement(purchase_id: int, *, user=None):
    p = RawMaterialTxn.objects.select_related("created_by").get(pk=purchase_id)
    if p.kind != RawMaterialTxn.Kind.PURCHASE:
        raise ValueError("PDF is only available for PURCHASE transactions.")

    ss = getattr(settings, "SITE_SETTINGS_OBJ", None)
    # If you have a getter (e.g., get_site_settings), use it:
    try:
        from core.utils_settings import get_site_settings
        ss = get_site_settings()
    except Exception:
        pass

    W, H = A4
    pdf_path = _purchase_pdf_path(p)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(f"RM Purchase - {p.supplier_name} - #{p.pk}")
    W, H = A4
    margin = 15 * mm
    avail = W - 2 * margin
    def fit_cols(widths):
        scale = min(1.0, float(avail) / float(sum(widths)))
        return [w * scale for w in widths]

    # Header
    # _draw_page_header(c, W, H, ss)
    # margin = 15 * mm
    # y = H - (30 * mm)
    # avail = W - 2 * margin
    # def fit_cols(widths):
    #     scale = min(1.0, float(avail) / float(sum(widths)))
    #     return [w * scale for w in widths]
    # -------- Brand header (logo left, company + address right) --------
    logo_path = None
    if ss and getattr(ss, "logo", None):
        try:
            logo_path = ss.logo.path
        except Exception:
            logo_path = None
    if not logo_path:
        lp = getattr(settings, "INVOICE_LOGO_PATH", "")
        logo_path = str(lp) if lp else None

    company_name = (ss.company_name if ss and getattr(ss, "company_name", None)
                    else getattr(settings, "COMPANY_NAME", "Your Company"))

    # address lines from SiteSettings (prefer list property/callable if you have it)
    addr_lines = getattr(ss, "company_address_list", None)
    if addr_lines is not None:
        addr_lines = addr_lines() if callable(addr_lines) else list(addr_lines)
    else:
        # fallback to a single text field
        addr_lines = _as_lines(getattr(ss, "company_address", "")) or \
                    _as_lines(getattr(settings, "COMPANY_ADDRESS", ""))

    top_y = H - margin
    logo_w, logo_h = (38 * mm, 18 * mm)
    if logo_path:
        try:
            c.drawImage(ImageReader(logo_path), margin, top_y - logo_h,
                        width=logo_w, height=logo_h, preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    # company block on the right
    _draw_multiline_right(
        c,
        [company_name] + addr_lines,
        W - margin,
        top_y,
        leading=14,
        font="Helvetica",
        size=10,
    )

    # leave space below header for the title
    y = top_y - (logo_h + 22)

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W/2, y, "Raw Material Purchase")
    y -= 18
    c.setFont("Helvetica", 12)
    c.drawCentredString(W/2, y, f"Supplier: {p.supplier_name or '—'}   ·   Date: {p.when:%Y-%m-%d}")
    y -= 20

    # --- Purchase summary (header row + single data row) ---
    kg        = D(str(p.qty_kg or 0)).quantize(D("0.000"))
    rate_int  = int(p.rate_pkr or 0)
    total_int = int(p.amount_pkr or 0)

    summary_headers = ["DC #", "Material", "Bags", "Qty (kg)", "Rate (PKR/kg)", "Total"]
    summary_values  = [
        p.dc_number or "—",
        (getattr(p, "material_type", "") or "—"),
        str(p.bags_count or 0),
        f"{kg:.3f}",
        pkr_str(rate_int),
        pkr_str(total_int),
    ]
    summary_data = [summary_headers, summary_values]

    # widths tuned for A4; run through fit_cols() so it never overflows
    summary_cols = fit_cols([70, 100, 60, 90, 110, 110])

    t = Table(summary_data, colWidths=summary_cols, repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))

    tw, th = t.wrapOn(c, W, H)
    if y - th < 90:
        _draw_footer(c, W, H, ss, bank_lines=_get_bank_lines(ss), ref=f"RM-Purchase-#{p.pk}", user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin

    t.drawOn(c, margin, y - th)
    y -= th + 18


    # --- Payments table ---
    rows, paid_total = _collect_purchase_payments(p)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Payments to Supplier")
    y -= 12

    pay_data = [["Date", "Method", "Bank / Reference", "Amount"]] + (rows or [["—", "—", "—", "—"]])
    pay_cols = fit_cols([90, 90, 240, 80])

    t2 = Table(pay_data, colWidths=pay_cols, repeatRows=1)
    t2.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    tw, th = t2.wrapOn(c, W, H)
    if y - th < 90:
        _draw_footer(c, W, H, ss, bank_lines=_get_bank_lines(ss), ref=f"RM-Purchase-#{p.pk}", user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t2.drawOn(c, margin, y - th)
    y -= th + 14

    # --- Totals bar (make sure to use paid_total from above) ---
    pending = int(p.amount_pkr or 0) - int(paid_total or 0)
    tot_data = [["Purchase Total", pkr_str(p.amount_pkr),
                "Paid Total", pkr_str(paid_total),
                "Pending", pkr_str(pending)]]
    tot_cols = fit_cols([110, 120, 110, 120, 110, 120])

    tot = Table(tot_data, colWidths=tot_cols, repeatRows=0)
    tot.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, -1), colors.black),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    tw, th = tot.wrapOn(c, W, H)
    if y - th < 90:
        _draw_footer(c, W, H, ss, bank_lines=_get_bank_lines(ss), ref=f"RM-Purchase-#{p.pk}", user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    tot.drawOn(c, margin, y - th)
    y -= th + 18


    # Footer
    _draw_footer(c, W, H, ss, bank_lines=_get_bank_lines(ss), ref=f"RM-Purchase-#{p.pk}", user=user)
    c.save()
    return str(pdf_path)
