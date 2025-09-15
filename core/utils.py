from pathlib import Path
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.units import mm
from datetime import date, datetime, timedelta
from calendar import monthrange
from django.db.models import Sum, Value, DecimalField, F, ExpressionWrapper
from django.db.models.functions import Coalesce
from .utils_weight import dkg  # your Decimal quantizer
# Models
from .models import Order, MaterialReceipt, Customer, CustomerMaterialLedger  # reuse the single dkg
from .models_ar import Payment
from core.models_ar import Payment, FINAL_STATES  # add this

# Money helpers (your new utilities)
from .utils_money import D, to_rupees_int, round_to

# ---- Optional: pull branding/settings from the singleton SiteSettings ----
try:
    from .utils_settings import get_site_settings
except Exception:
    def get_site_settings():
        return None

# ----------------------------
# Formatting helpers (display)
# ----------------------------
def pkr_str(val) -> str:
    """
    Format any Decimal/number as integer-rupees string with PKR prefix.
    Uses your to_rupees_int helper for rounding (0 decimals).
    """
    return f"Rs. {to_rupees_int(D(val)):,}"

# ---- Customer Billing Monthly ----------
# Apply table style
style = TableStyle([
    ("GRID", (0, 0), (-1, -2), 0.5, colors.black),

    # Header row
    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),

    # Total row (last row)
    ("BACKGROUND", (0, -1), (-1, -1), colors.black),
    ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),

    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
])
# ----------------------------
# Small drawing helpers
# ----------------------------
def _draw_page_header(c, W, H, ss):
    margin = 15 * mm
    c.setFont("Helvetica-Bold", 11)
    company_name = (ss.company_name if ss and getattr(ss, "company_name", None)
                    else getattr(settings, "COMPANY_NAME", "Your Company"))
    c.drawString(margin, H - margin + 5, company_name)

def _as_lines(v):
    """Split string (or list) into clean non-empty lines."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).splitlines() if s.strip()]
def _draw_notes_box(c, W, H, ss, y, *, margin=15*mm):
    """
    Draw a notes block (from SiteSettings.notes) above the footer and
    return the new y. Breaks the page if there isn't enough room.
    """
    notes_lines = []
    if ss:
        nl = getattr(ss, "notes_list", None)
        if nl is not None:
            notes_lines = _as_lines(nl() if callable(nl) else nl)
        else:
            notes_lines = _as_lines(getattr(ss, "notes", ""))
    if not notes_lines:
        return y  # nothing to draw

    avail_w   = W - 2*margin
    pad       = 20   # px
    line_h    = 15  # line height
    box_h     = line_h*len(notes_lines) + pad*1.25
    min_footer_gap = 90  # keep same safety margin you use elsewhere

    # New page if the box won't fit
    if y - box_h < min_footer_gap:
        # caller should handle footer/page-break if needed,
        # but we can still start a new page here defensively:
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin

    # border box (subtle)
    c.setStrokeColor(colors.lightgrey)
    c.setFillColor(colors.black)
    c.roundRect(margin-2, y - box_h, avail_w + 4, box_h, 4, stroke=1, fill=0)

    # title (optional). Comment out if you don't want a label.
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y - 10, "Notes")

    # text lines
    c.setFont("Helvetica", 9)
    # top of text area (inside padding)
    text_top_y = y - pad
    end_y = _draw_multiline_left(
        c, notes_lines, margin, text_top_y, leading=line_h, font="Helvetica", size=9
    )

    # small gap below the box
    return end_y - 8

def _draw_footer(
    c, W, H, ss, *, payment_terms: str | None = None,
    bank_lines: list[str] | None = None, ref: str | None = None, user=None,
):
    margin = 15 * mm

    company_name = (
        ss.company_name
        if ss and getattr(ss, "company_name", None)
        else getattr(settings, "COMPANY_NAME", "Your Company")
    )
    who = "System"
    if user and getattr(user, "is_authenticated", False):
        who = (getattr(user, "get_full_name", lambda: "")() or
               getattr(user, "get_username", lambda: "")() or
               str(user)).strip() or "System"
    stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")

    # --- Left column content (company, terms, notes, banks) ---
    left_top_y = 35 * mm
    left_lines = [str(company_name)]

    if (payment_terms or "").strip():
        left_lines.append(f"Payment Terms: {payment_terms.strip()}")

    # NEW: notes from Site Settings (prefer a helper .notes_list if you have one)
    notes_lines = []
    if ss:
        if hasattr(ss, "notes_list"):         # if you made a helper like bank_details_list
            notes_lines = _as_lines(ss.notes_list)
        else:
            notes_lines = _as_lines(getattr(ss, "notes", ""))

    # Bank details (what you already had)
    if bank_lines:
        left_lines.extend(_as_lines(bank_lines))
    else:
        # optional: fallback to settings if caller didn’t pass bank_lines
        default_bank = getattr(settings, "BANK_DETAILS_LINES", [])
        left_lines.extend(_as_lines(default_bank))

    left_end_y = _draw_multiline_left(
        c, left_lines, margin, left_top_y, leading=12, font="Helvetica", size=8.5
    )

    # --- Baseline + right-side meta ---
    default_baseline = 12 * mm
    baseline = max(8 * mm, min(default_baseline, left_end_y - 2 * mm))

    c.setFont("Helvetica-Oblique", 8)
    right_txt = f"Generated by {who} · {stamp}"
    if ref:
        right_txt = f"{right_txt} · {ref}"
    c.drawRightString(W - margin, baseline, right_txt)

def _statement_path(customer: Customer, year: int, month: int) -> Path:
    base = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    base.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in customer.company_name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return base / f"Statement-{safe_name}-{year:04d}-{month:02d}.pdf"

def _period_bounds(year: int, month: int):
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])

    # For DateTimeField filtering (full range with tz-aware datetimes)
    start_dt = datetime.combine(first, datetime.min.time(), tzinfo=timezone.get_current_timezone())
    end_dt   = datetime.combine(last, datetime.max.time(), tzinfo=timezone.get_current_timezone())

    return first, last, start_dt, end_dt

# ---------- RANGE HELPERS (add below _period_bounds) ----------
def _period_bounds_from_dates(start_date: date, end_date: date):
    """
    Convert plain dates into:
      first, last (DATEs)
      period_start, period_end (TZ-aware datetimes spanning the full days)
    """
    if start_date > end_date:
        start_date, end_date = end_date, start_date  # swap defensively

    tz = timezone.get_current_timezone()
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
    end_dt   = datetime.combine(end_date,   datetime.max.time(), tzinfo=tz)
    return start_date, end_date, start_dt, end_dt


def _statement_path_range(customer: Customer, start: date, end: date) -> Path:
    """
    File name for arbitrary date range. Example:
      Statement-Customer-2025-01-01_to_2025-03-31.pdf
    """
    base = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    base.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch for ch in customer.company_name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    return base / f"Statement-{safe_name}-{start.isoformat()}_to_{end.isoformat()}.pdf"


def _nice_range_label(start, end):
    """
    Human-friendly label for a date range.
    Works with datetime/date/str.
    """
    def _fmt(d):
        if isinstance(d, str):
            return d
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            return d.strftime("%d %b %Y")
        return str(d)

    return f"{_fmt(start)} → {_fmt(end)}"


def generate_customer_statement_range(customer_id: int, start_date: date, end_date: date, user=None) -> str:
    """
    Same statement as monthly, but for an arbitrary date range [start_date .. end_date].
    Keeps your existing tables & math. Only the period window and labels change.
    """
    # --- Setup & Period ---
    customer = Customer.objects.get(id=customer_id)
    ss = get_site_settings()

    first, last, period_start, period_end = _period_bounds_from_dates(start_date, end_date)
    day_before = first - timedelta(days=1)
    period_label = _nice_range_label(first, last)

    # --- Opening A/R (money) BEFORE this period ---
    charges_before_pkr = sum(
        int(getattr(o, "grand_total_pkr", 0) or 0)
        for o in Order.objects.filter(customer=customer, status__in=FINAL_STATES, order_date__lte=day_before)
    )
    payments_before_pkr = sum(
        int(D(p.amount or 0))
        for p in Payment.objects.filter(customer=customer, received_on__lte=day_before)
    )
    carry_pkr = int(customer.previous_pending_balance_pkr or 0)
    opening_due_pkr = carry_pkr + charges_before_pkr - payments_before_pkr

    # --- Invoices in range (ALL non-draft) ---
    orders = (
        Order.objects
        .select_related("customer")
        .prefetch_related("payment_allocations__payment")
        .filter(customer=customer, order_date__range=(first, last))
        .exclude(status="DRAFT")
        .order_by("order_date", "id")
    )

    rows_orders = []
    total_qty   = Decimal("0.000")
    total_inv   = Decimal("0.00")
    total_paid  = Decimal("0.00")
    total_due   = Decimal("0.00")
    charges_period_pkr = 0
    payments_period_pkr = 0

    for o in orders:
        qty_kg = dkg(getattr(o, "target_total_kg", 0) or 0)
        rate   = D(getattr(o, "price_per_kg", 0) or 0)
        invoice_total = D(o.grand_total)

        allocs = getattr(o, "payment_allocations", None)
        paid_raw = sum((D(a.amount or 0)) for a in allocs.all()) if allocs is not None else Decimal("0.00")
        paid = D(paid_raw)
        due = D(invoice_total - paid)

        total_qty  += qty_kg
        total_inv  += invoice_total
        total_paid += paid
        total_due  += due
        charges_period_pkr += int(getattr(o, "grand_total_pkr", 0) or 0)

        rows_orders.append([
            str(getattr(o, "delivery_challan_date", "") or getattr(o, "order_date", "") or ""),
            getattr(o, "invoice_number", f"INV{o.id}") or "",
            str(getattr(o, "delivery_challan", "") or ""),
            str(getattr(o, "roll_size", "") or ""),
            f"{qty_kg:,.3f}",
            dkg(rate),
            pkr_str(invoice_total),
            pkr_str(paid),
            pkr_str(due),
        ])

    rows_orders.append(["", "", "", "Totals", f"{total_qty:,.3f}", "", pkr_str(total_inv), pkr_str(total_paid), pkr_str(total_due)])

    # --- Payments in range ---
    payments_qs = Payment.objects.filter(customer=customer, received_on__range=(first, last)).order_by("received_on", "id")
    rows_payments = []
    payments_total = Decimal("0.00")
    for p in payments_qs:
        amt = D(p.amount or 0)
        payments_total += amt
        payments_period_pkr += int(amt)
        note_bits = []
        if getattr(p, "reference", ""): note_bits.append(str(p.reference))
        if getattr(p, "notes", ""    ): note_bits.append(str(p.notes))
        notes = " · ".join(note_bits)
        rows_payments.append([str(getattr(p, "received_on", "") or ""), str(getattr(p, "method", "") or ""), notes or "—", pkr_str(amt)])
    rows_payments.append(["", "", "Total", pkr_str(payments_total)])

    # --- Material movement detail (both + and −) in range ---
    rows_receipts = []
    receipts_total = Decimal("0.000")
    ledger_entries = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__range=(period_start, period_end))
        .order_by("date", "id")
    )
    for rec in ledger_entries:
        qty = dkg(Decimal(rec.delta_kg or 0))
        rows_receipts.append([rec.date.date().isoformat(), str(rec.memo or "—"), f"{qty:,.3f}"])
        receipts_total += qty
    rows_receipts = rows_receipts or [["—", "—", "—"]]
    if rows_receipts and rows_receipts[0][0] != "—":
        rows_receipts.append(["", "Total", f"{dkg(receipts_total):,.3f}"])

    closing_due_pkr = int(opening_due_pkr) + int(charges_period_pkr) - int(payments_period_pkr)

    # --- Material balance summary (ledger only) for range ---
    KG  = DecimalField(max_digits=12, decimal_places=3)
    kg0 = Decimal("0.000")

    opening_kg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__lt=period_start)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )
    in_month_in_kg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__range=(period_start, period_end), delta_kg__gt=0)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )
    in_month_out_neg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__range=(period_start, period_end), delta_kg__lt=0)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )
    used_out_kg = -in_month_out_neg
    closing_kg = opening_kg + in_month_in_kg + in_month_out_neg

    rows_mat_balance = [
        ["Opening Balance (kg)", f"{dkg(opening_kg):,.3f}"],
        ["Received IN (kg)",     f"{dkg(in_month_in_kg):,.3f}"],
        ["Used OUT (kg)",        f"{dkg(used_out_kg):,.3f}"],
        ["Closing Balance (kg)", f"{dkg(closing_kg):,.3f}"],
    ]

    # --- PDF: header & layout (labels adjusted for date span) ---
    pdf_path = _statement_path_range(customer, first, last)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    title = f"Statement-{customer.company_name}-{first.isoformat()}_to_{last.isoformat()}"
    c.setTitle(title)
    W, H = A4
    margin = 15 * mm
    right_x = W - margin
    top_y = H - margin

    # Logo/header (unchanged from your monthly)
    logo_path = None
    ss = get_site_settings()
    if ss and getattr(ss, "logo", None):
        try:
            logo_path = ss.logo.path
        except Exception:
            logo_path = None
    if not logo_path:
        lp = getattr(settings, "INVOICE_LOGO_PATH", "")
        logo_path = str(lp) if lp else None

    logo_w, logo_h = (38 * mm, 18 * mm)
    if logo_path:
        try:
            c.drawImage(ImageReader(logo_path), margin, top_y - logo_h, width=logo_w, height=logo_h,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    company_name = (ss.company_name if ss and ss.company_name else getattr(settings, "COMPANY_NAME", "Your Company"))
    company_addr_lines = (
        ss.company_address_list if ss and hasattr(ss, "company_address_list")
        else getattr(settings, "COMPANY_ADDRESS_LINES", [])
    )
    _draw_multiline_right(c, [company_name] + list(_as_lines(company_addr_lines)), right_x, top_y, leading=14, font="Helvetica", size=10)

    title_y = top_y - (logo_h + 35 if logo_path else 55)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(W/2, title_y, f"{customer.company_name}")

    meta_y = title_y - 22
    c.setFont("Helvetica", 14)
    c.drawCentredString(W/2, meta_y, f"Statement ({period_label})")

    # Reuse your table drawing code exactly as in monthly:
    y = meta_y - 24

    # Table 1: Invoices
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Invoices (All)")
    y -= 12
    orders_table_data = [["Date", "Invoice #", "DC #", "Size", "Total Qty (kg)", "Rate", "Invoice Total", "Paid", "Amount Due"]] + (rows_orders or [["—"] * 9])
    orders_base_cols = [70, 85, 50, 50, 85, 55, 80, 80, 80]
    avail = W - 2 * margin
    scale = min(1.0, avail / float(sum(orders_base_cols)))
    orders_col_widths = [w * scale for w in orders_base_cols]
    t1 = Table(orders_table_data, colWidths=orders_col_widths, repeatRows=1); t1.setStyle(style)
    tw, th = t1.wrapOn(c, W, H); x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t1.drawOn(c, x_center, y - th); y = y - th - 30

    # Table 2: Payments (Period)
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Payments Received (Period)")
    y -= 12
    payments_table = [["Date", "Method", "Notes / Reference", "Amount"]] + (rows_payments or [["—"] * 4])
    pay_base_cols = [90, 90, 260, 80]; scale = min(1.0, (W - 2 * margin) / float(sum(pay_base_cols))); pay_col_widths = [w * scale for w in pay_base_cols]
    t2 = Table(payments_table, colWidths=pay_col_widths, repeatRows=1); t2.setStyle(style)
    tw, th = t2.wrapOn(c, W, H); x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t2.drawOn(c, x_center, y - th); y = y - th - 30

    # Table 3: Material detail (Period)
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Material Movements (Period)")
    y -= 12
    receipts_table = [["Date", "Notes / Reference", "Qty (kg)"]] + (rows_receipts or [["—"] * 3])
    rec_base_cols = [90, 300, 100]; scale = min(1.0, (W - 2 * margin) / float(sum(rec_base_cols))); rec_col_widths = [w * scale for w in rec_base_cols]
    t3 = Table(receipts_table, colWidths=rec_col_widths, repeatRows=1); t3.setStyle(style)
    tw, th = t3.wrapOn(c, W, H); x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t3.drawOn(c, x_center, y - th); y = y - th - 30

    # Material Balance Summary
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Material Balance Summary")
    y -= 12
    mat_base_cols = [220, 100]; scale = min(1.0, (W - 2 * margin) / float(sum(mat_base_cols))); mat_col_widths = [w * scale for w in mat_base_cols]
    t3 = Table(rows_mat_balance, colWidths=mat_col_widths, repeatRows=0); t3.setStyle(style)
    tw, th = t3.wrapOn(c, W, H); x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t3.drawOn(c, x_center, y - th); y = y - th - 30

    # Bill Summary (range)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, f"Bill Summary ({period_label})")
    y -= 12
    closing_label = "Balance due (carry to next bill)" if closing_due_pkr >= 0 else "Advance on account (credit)"
    closing_value = abs(int(closing_due_pkr))
    summary_rows = [
        [f"Opening balance (before {first.strftime('%b %Y')})", pkr_str(opening_due_pkr)],
        ["+ Charges in period",                                 pkr_str(charges_period_pkr)],
        ["− Payments received in period",                       pkr_str(payments_period_pkr)],
        [closing_label,                                         pkr_str(closing_value)],
    ]
    sum_base_cols = [300, 140]; scale = min(1.0, (W - 2 * margin) / float(sum(sum_base_cols))); sum_col_widths = [w * scale for w in sum_base_cols]
    t_sum = Table(summary_rows, colWidths=sum_col_widths, repeatRows=0); t_sum.setStyle(style)
    tw, th = t_sum.wrapOn(c, W, H); x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)
        c.showPage(); _draw_page_header(c, W, H, ss); y = H - margin
    t_sum.drawOn(c, x_center, y - th)

    y = y - th - 20
    if ss and (getattr(ss, "notes", "") or getattr(ss, "notes_list", None)):
        y = _draw_notes_box(c, W, H, ss, y)

    bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list")) else getattr(settings, "BANK_DETAILS_LINES", []))
    _draw_footer(c, W, H, ss, bank_lines=bank_lines, ref=title, user=user)

    c.save()
    return str(pdf_path)


################################################
## Generate Monthly Customer Billing
################################################
def generate_customer_monthly_statement(customer_id: int, year: int, month: int, user=None) -> str:
    """
    Monthly statement for a customer:
      • Table 1: ALL invoices in the month (paid + unpaid) with Paid and Due columns.
      • Table 2: Payments received during the month (date, method, notes/ref, amount).
      • Table 3: Material balance summary (Opening, IN, OUT, Closing).
    """
    # --- Setup & Period ---
    customer = Customer.objects.get(id=customer_id)
    ss = get_site_settings()
    first, last, period_start, period_end = _period_bounds(year, month)
     # --- Opening (before this month): carry + final invoices ≤ last_month_day − payments ≤ last_month_day ---
    day_before = first - timedelta(days=1)

    # final/billable orders up to the day before this month
    charges_before_pkr = sum(
        int(getattr(o, "grand_total_pkr", 0) or 0)
        for o in Order.objects.filter(customer=customer, status__in=FINAL_STATES, order_date__lte=day_before)
    )
    # all payments up to the day before this month (as int PKR)
    payments_before_pkr = sum(int(D(p.amount or 0)) for p in Payment.objects.filter(customer=customer, received_on__lte=day_before))

    carry_pkr = int(customer.previous_pending_balance_pkr or 0)
    opening_due_pkr = carry_pkr + charges_before_pkr - payments_before_pkr

      # --- Invoices (ALL non-draft in period) ---
    orders = (
        Order.objects
        .select_related("customer")
        .prefetch_related("payment_allocations__payment")
        .filter(customer=customer, order_date__range=(first, last))
        .exclude(status="DRAFT")
        .order_by("order_date", "id")
    )

    rows_orders = []
    total_qty   = Decimal("0.000")
    total_inv   = Decimal("0.00")
    total_paid  = Decimal("0.00")
    total_due   = Decimal("0.00")
    charges_period_pkr = 0      # add
    payments_period_pkr = 0   # add


    for o in orders:
        qty_kg = dkg(getattr(o, "target_total_kg", 0) or 0)
        rate   = D(getattr(o, "price_per_kg", 0) or 0)

        # model computes GST if applicable
        invoice_total = D(o.grand_total)

        # Paid = sum of allocations applied to THIS order
        allocs = getattr(o, "payment_allocations", None)
        paid_raw = sum((D(a.amount or 0)) for a in allocs.all()) if allocs is not None else Decimal("0.00")
        paid = D(paid_raw)

        due = D(invoice_total - paid)
        total_qty  += qty_kg
        total_inv  += invoice_total
        total_paid += paid
        total_due  += due
        charges_period_pkr += int(getattr(o, "grand_total_pkr", 0) or 0)  # add

        rows_orders.append([
            str(getattr(o, "delivery_challan_date", "") or getattr(o, "order_date", "") or ""),
            getattr(o, "invoice_number", f"INV{o.id}") or "",
            str(getattr(o, "delivery_challan", "") or ""),
            str(getattr(o, "roll_size", "") or ""),
            f"{qty_kg:,.3f}",
            dkg(rate),                 # Rate (money) -> PKR int format
            pkr_str(invoice_total),        # Invoice Total
            pkr_str(paid),                 # Paid
            pkr_str(due),                  # Amount Due
        ])

    # Totals row for invoices
    rows_orders.append([
        "", "", "", "Totals",
        f"{total_qty:,.3f}",
        "",
        pkr_str(total_inv),
        pkr_str(total_paid),
        pkr_str(total_due),
    ])

    # --- Payments received in the month ---
    payments_qs = (
        Payment.objects
        .filter(customer=customer, received_on__range=(first, last))
        .order_by("received_on", "id")
    )
    rows_payments = []
    payments_total = Decimal("0.00")
    for p in payments_qs:
        amt = D(p.amount or 0)
        payments_total += amt
        payments_period_pkr += int(D(p.amount or 0))   # add (keeps your theme; you already list p.amount)

        note_bits = []
        if getattr(p, "reference", ""):
            note_bits.append(str(p.reference))
        if getattr(p, "notes", ""):
            note_bits.append(str(p.notes))
        notes = " · ".join(note_bits)
        rows_payments.append([
            str(getattr(p, "received_on", "") or ""),
            str(getattr(p, "method", "") or ""),
            notes or "—",
            pkr_str(amt),                  # Money -> PKR
        ])
    rows_payments.append(["", "", "Total", pkr_str(payments_total)])
# --- Material Received (Month) — LEDGER IN ONLY ---
    rows_receipts = []
    receipts_total = Decimal("0.000")

    ledger_entries = (
    CustomerMaterialLedger.objects
    .filter(customer=customer, date__range=(period_start, period_end))
    .order_by("date", "id")
)

    rows_receipts = []
    receipts_total = Decimal("0.000")

    for rec in ledger_entries:
        qty = dkg(Decimal(rec.delta_kg or 0))
        rows_receipts.append([
            rec.date.date().isoformat(),
            str(rec.memo or "—"),
            f"{qty:,.3f}",
        ])
        receipts_total += qty

    rows_receipts = rows_receipts or [["—", "—", "—"]]
    if rows_receipts and rows_receipts[0][0] != "—":
        rows_receipts.append(["", "Total", f"{dkg(receipts_total):,.3f}"])

    closing_due_pkr = int(opening_due_pkr) + int(charges_period_pkr) - int(payments_period_pkr)

    # ===========================
    # MATERIAL BALANCE SUMMARY — LEDGER ONLY
    # Closing = Opening + IN − OUT
    # ===========================
    KG  = DecimalField(max_digits=12, decimal_places=3)
    kg0 = Decimal("0.000")

    # Opening = Σ(ledger before this month)
    opening_kg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__lt=period_start)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )

    # This month IN (positive deltas)
    in_month_in_kg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__range=(period_start, period_end), delta_kg__gt=0)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )

    # This month OUT (sum is negative → display positive)
    in_month_out_neg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer, date__range=(period_start, period_end), delta_kg__lt=0)
        .aggregate(v=Coalesce(Sum("delta_kg", output_field=KG), Value(kg0, output_field=KG)))
        ["v"] or kg0
    )
    used_out_kg = -in_month_out_neg

    closing_kg = opening_kg + in_month_in_kg + in_month_out_neg

    rows_mat_balance = [
        ["Opening Balance (kg)", f"{dkg(opening_kg):,.3f}"],
        ["Received IN (kg)",     f"{dkg(in_month_in_kg):,.3f}"],
        ["Used OUT (kg)",        f"{dkg(used_out_kg):,.3f}"],
        ["Closing Balance (kg)", f"{dkg(closing_kg):,.3f}"],
    ]
    # --- PDF: header & layout ---
    pdf_path = _statement_path(customer, year, month)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    # 👉 Set the title metadata here
    title = f"Statement-{customer.company_name}-{year:04d}-{month:02d}"
    c.setTitle(title)
    W, H = A4
    margin = 15 * mm
    right_x = W - margin
    top_y = H - margin

    # Logo / header
    logo_path = None
    ss = get_site_settings()
    if ss and getattr(ss, "logo", None):
        try:
            logo_path = ss.logo.path
        except Exception:
            logo_path = None
    if not logo_path:
        lp = getattr(settings, "INVOICE_LOGO_PATH", "")
        logo_path = str(lp) if lp else None

    logo_w, logo_h = (38 * mm, 18 * mm)
    if logo_path:
        try:
            c.drawImage(ImageReader(logo_path), margin, top_y - logo_h,
                        width=logo_w, height=logo_h,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    company_name = (ss.company_name if ss and ss.company_name else getattr(settings, "COMPANY_NAME", "Your Company"))
    company_addr_lines = (
        ss.company_address_list if ss and hasattr(ss, "company_address_list")
        else getattr(settings, "COMPANY_ADDRESS_LINES", [])
    )
    _draw_multiline_right(c, [company_name] + list(_as_lines(company_addr_lines)),
                          right_x, top_y, leading=14, font="Helvetica", size=10)

    # Title & meta
    title_y = top_y - (logo_h + 35 if logo_path else 55)
    c.setFont("Helvetica-Bold", 16)
    # c.drawCentredString(W/2, title_y, "Customer Monthly Statement")

    # Title positions
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(W/2, title_y, f"{customer.company_name}")

    # Move down for "Monthly Statement"
    # Format month name instead of "08"
    import calendar
    month_name = calendar.month_name[month]  # e.g. "August"
    meta_y = title_y - 22
    c.setFont("Helvetica", 14)
    c.drawCentredString(W/2, meta_y, f"{month_name} {year} Statement")


    # Set y for further content
    y = meta_y - 24
    
    # --- Table 1: Invoices (All) ---
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Invoices (All)")
    y -= 12

    orders_table_data = [[
        "Date", "Invoice #", "DC #", "Size", "Total Qty (kg)", "Rate", "Invoice Total", "Paid", "Amount Due"
    ]] + (rows_orders or [["—"] * 9])

    orders_base_cols = [70, 85, 50, 50, 85, 55, 80, 80, 80]
    avail = W - 2 * margin
    scale = min(1.0, avail / float(sum(orders_base_cols)))
    orders_col_widths = [w * scale for w in orders_base_cols]

    t1 = Table(orders_table_data, colWidths=orders_col_widths, repeatRows=1)
    t1.setStyle(style)

    tw, th = t1.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin
    t1.drawOn(c, x_center, y - th)
    y = y - th - 30

    # --- Table 2: Payments Received (Month) ---
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Payments Received (Month)")
    y -= 12

    payments_table = [["Date", "Method", "Notes / Reference", "Amount"]] + (rows_payments or [["—"] * 4])
    pay_base_cols = [90, 90, 260, 80]
    scale = min(1.0, (W - 2 * margin) / float(sum(pay_base_cols)))
    pay_col_widths = [w * scale for w in pay_base_cols]

    t2 = Table(payments_table, colWidths=pay_col_widths, repeatRows=1)
    t2.setStyle(style)
    

    tw, th = t2.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin
    t2.drawOn(c, x_center, y - th)
    y = y - th - 30

    # --- Table 3: Material Balance Summary ---
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin


    # --- Table: Material Received (Month) ---
    if y < 120:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                    else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                    ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Material Received (Month)")
    y -= 12

    receipts_table = [["Date", "Notes / Reference", "Qty (kg)"]] + (rows_receipts or [["—"] * 3])
    rec_base_cols = [90, 300, 100]
    scale = min(1.0, (W - 2 * margin) / float(sum(rec_base_cols)))
    rec_col_widths = [w * scale for w in rec_base_cols]

    t3 = Table(receipts_table, colWidths=rec_col_widths, repeatRows=1)
    t3.setStyle(style)

    tw, th = t3.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                    else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                    ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin
    t3.drawOn(c, x_center, y - th)
    y = y - th - 30

    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Material Balance Summary")
    y -= 12

    mat_base_cols = [220, 100]
    scale = min(1.0, (W - 2 * margin) / float(sum(mat_base_cols)))
    mat_col_widths = [w * scale for w in mat_base_cols]

    t3 = Table(rows_mat_balance, colWidths=mat_col_widths, repeatRows=0)
    t3.setStyle(style)

    tw, th = t3.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin
    t3.drawOn(c, x_center, y - th)
    y = y - th - 30
    # === Bill Summary (this month) ===
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, f"Bill Summary ({month_name} {year})")
    y -= 12

    # show "Advance (credit)" instead of a minus
    closing_label = "Balance due (carry to next bill)" if closing_due_pkr >= 0 else "Advance on account (credit)"
    closing_value  = abs(int(closing_due_pkr))

    summary_rows = [
        [f"Opening balance (before {month_name} {year})", pkr_str(opening_due_pkr)],
        ["+ Charges this month",                                  pkr_str(charges_period_pkr)],
        ["− Payments received this month",                         pkr_str(payments_period_pkr)],
        [closing_label,                                           pkr_str(closing_value)],
    ]
    sum_base_cols = [300, 140]
    scale = min(1.0, (W - 2 * margin) / float(sum(sum_base_cols)))
    sum_col_widths = [w * scale for w in sum_base_cols]

    t_sum = Table(summary_rows, colWidths=sum_col_widths, repeatRows=0)
    t_sum.setStyle(style)

    tw, th = t_sum.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0
    
    if y - th < 90:
    
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin
    t_sum.drawOn(c, x_center, y - th)
   
    y = y - th - 20
    if ss and (getattr(ss, "notes", "") or getattr(ss, "notes_list", None)):
        y = _draw_notes_box(c, W, H, ss, y)
    # --- Final footer ---
    # Draw Site Settings notes in the body area (above the footer)
    
       
    bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                  else getattr(settings, "BANK_DETAILS_LINES", []))
    _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                 ref=f"Statement-{customer.company_name}-{year:04d}-{month:02d}", user=user)

    c.save()
    return str(pdf_path)


################################################
## End Customer Billing Monthly
################################################


################################################
## INVOICE (SINGLE ORDER) PDF
################################################
def _invoice_path(order: Order) -> Path:
    inv_no = order.invoice_number or f"SSP{order.id:05d}"
    base = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{inv_no}.pdf"

def _as_lines(text_or_list):
    if not text_or_list:
        return []
    if isinstance(text_or_list, (list, tuple)):
        return [str(x).strip() for x in text_or_list if str(x).strip()]
    return [ln.strip() for ln in str(text_or_list).splitlines() if ln.strip()]

def _draw_multiline_left(c, lines, x, y, leading=12, font="Helvetica", size=9):
    c.setFont(font, size)
    cur_y = y
    for ln in _as_lines(lines):
        c.drawString(x, cur_y, ln)
        cur_y -= leading
    return cur_y

def _draw_multiline_right(c, lines, x_right, y, leading=12, font="Helvetica", size=9):
    c.setFont(font, size)
    cur_y = y
    for ln in _as_lines(lines):
        c.drawRightString(x_right, cur_y, ln)
        cur_y -= leading
    return cur_y

def _as_decimal(x):
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def _draw_status_stamp(c, text, rgb_hex, W, H):
    c.saveState()
    col = colors.HexColor(rgb_hex)
    c.translate(W/2, H/2)
    c.rotate(30)
    c.setStrokeColor(col)
    c.setFillColor(col)
    c.setLineWidth(3)
    stamp_font = "Helvetica-Bold"
    font_size = 46
    c.setFont(stamp_font, font_size)
    txt_w = c.stringWidth(text, stamp_font, font_size)
    c.drawString(-txt_w/2, -10, text)
    c.restoreState()

def generate_invoice(order_id, user=None, out_path=None):
    order = (
        Order.objects
        .select_related("customer")
        .prefetch_related("payment_allocations__payment")
        .get(id=order_id)
    )

    rolls_mgr = getattr(order, 'rolls', None) or getattr(order, 'orderroll_set', None)
    rolls = list(rolls_mgr.all()) if callable(getattr(rolls_mgr, 'all', None)) else []

    ss = get_site_settings()

    inv_no = order.invoice_number or f"Invoice-SSP-{order.id:05d}"
    pdf_path = Path(out_path) if out_path else _invoice_path(order)

    def _dec_attr(obj, name, default=Decimal("0")) -> Decimal:
        val = getattr(obj, name, default)
        if callable(val):
            val = val()
        try:
            return Decimal(str(val))
        except Exception:
            return Decimal("0")

    before_balance = dkg(_dec_attr(order.customer, "material_balance_kg"))

    target_kg_raw = _dec_attr(order, "target_total_kg") or _dec_attr(order, "target_weight_kg")
    target_kg = dkg(target_kg_raw)

    status = (getattr(order, "status", "") or "").upper()
    if status in {"CONFIRMED", "INPROD", "CLOSED"}:
        after_balance = dkg(before_balance - target_kg)
    else:
        after_balance = before_balance

    # --- Pricing (bill on target) ---
    price_per_kg = D(_dec_attr(order, "price_per_kg"))
    subtotal = round_to(target_kg * price_per_kg, 2)

    # Tax
    tax_rate = Decimal("0")
    tax_label = "Tax"
    if getattr(order, "include_gst", True):
        if ss and ss.tax_rate is not None:
            tax_rate = (D(ss.tax_rate) / Decimal("100"))
            tax_label = ss.tax_label or "Tax"
        else:
            tax_rate = D(getattr(settings, "TAX_RATE", 0) or 0)
            tax_label = getattr(settings, "TAX_LABEL", "Tax")
    tax_amount  = round_to(subtotal * tax_rate, 2) if tax_rate else Decimal("0.00")
    grand_total = round_to(subtotal + tax_amount, 2)

    # Payments
    try:
        allocations = list(order.payment_allocations.select_related("payment").order_by("payment__received_on","id"))
    except Exception:
        allocations = []
    amount_paid = sum(D(_as_decimal(getattr(a, "amount", 0))) for a in allocations)
    amount_paid = round_to(amount_paid, 2)
    balance_due = round_to(grand_total - amount_paid, 2)

    # ---- STATUS STAMP ----
    if balance_due == Decimal("0.00"):
        status_text, status_color = "FULLY PAID", "#22c55e"
    else:
        is_overdue = False
        try:
            if order.delivery_date and order.delivery_date < date.today():
                is_overdue = True
        except Exception:
            is_overdue = False
        if is_overdue:
            status_text, status_color = "OVERDUE", "#ef4444"
        elif amount_paid > Decimal("0.00"):
            status_text, status_color = "PARTIALLY PAID", "#f59e0b"
        else:
            status_text, status_color = "PENDING", "#3b82f6"

    # --- PDF setup ---
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(inv_no)
    W, H = A4
    margin = 15 * mm
    top_y = H - margin
    right_x = W - margin
    _draw_status_stamp(c, status_text, status_color, W, H)

    # Header: logo + company block
    logo_path = None
    if ss and getattr(ss, "logo", None):
        try:
            logo_path = ss.logo.path
        except Exception:
            logo_path = None
    if not logo_path:
        lp = getattr(settings, "INVOICE_LOGO_PATH", "")
        logo_path = str(lp) if lp else None

    logo_w, logo_h = (38 * mm, 18 * mm)
    if logo_path:
        try:
            c.drawImage(ImageReader(logo_path), margin, top_y - logo_h, width=logo_w, height=logo_h,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            pass

    company_name = (ss.company_name if ss and ss.company_name else getattr(settings, "COMPANY_NAME", "Your Company"))
    company_addr_lines = (
        ss.company_address_list if ss and hasattr(ss, "company_address_list")
        else getattr(settings, "COMPANY_ADDRESS_LINES", [])
    )
    _draw_multiline_right(c, [company_name] + list(_as_lines(company_addr_lines)),
                          right_x, top_y, leading=14, font="Helvetica", size=10)

    # Center title + meta
    title_y = top_y - (logo_h + 35 if logo_path else 55)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W / 2, title_y, "INVOICE")

    meta_y = title_y - 28
    c.setFont("Helvetica", 10)
    c.drawCentredString(
        W / 2, meta_y,
        f"Invoice No: {inv_no}    |    Order Date: {order.order_date}    |    Delivery Date: {order.delivery_date}"
    )

    # Customer block
    cust = order.customer
    cust_y = meta_y - 35
    cust_lines = [f"Bill To: {cust.company_name}"]
    if getattr(cust, "contact_name", ""): cust_lines.append(f"Attn: {cust.contact_name}")
    if getattr(cust, "address", ""):      cust_lines.append(cust.address)
    if getattr(cust, "phone", ""):        cust_lines.append(f"Phone: {cust.phone}")
    if getattr(cust, "email", ""):        cust_lines.append(f"Email: {cust.email}")

    c.setFont("Helvetica", 10)
    y = cust_y
    for ln in cust_lines:
        c.drawCentredString(W / 2, y, ln)
        y -= 14

    # --- SUMMARY TABLE (one row) ---
    table_top_y = y - 16
    size_val = getattr(order, "size", getattr(order, "roll_size", ""))
    micron_val = getattr(order, "micron", "")

    data = [["Size", "Micron", "Target Weight (kg)", "Price/Kg", "Line Total"]]
    data.append([
        str(size_val),
        str(micron_val),
        f"{target_kg:,.3f}",
        dkg(price_per_kg),       # money -> PKR
        pkr_str(subtotal),           # money -> PKR
    ])

    col_widths = [140, 80, 120, 90, 95]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9eef6")),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),

        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("ALIGN", (0, 1), (-1, -1), "CENTER"),

        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))

    avail_height = table_top_y - 160
    tw, th = tbl.wrapOn(c, W - 2 * margin, avail_height)
    y_cursor = table_top_y
    tbl.drawOn(c, margin, y_cursor - th)
    y_cursor = y_cursor - th - 14

    # --- ROLL WEIGHTS LIST ---
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y_cursor, "Roll Weights:")
    y_cursor -= 14
    c.setFont("Helvetica", 9)

    if rolls:
        numbers = []
        for idx, r in enumerate(rolls, start=1):
            w = dkg(_as_decimal(getattr(r, 'weight_kg', 0)))
            numbers.append(f"{idx}) {w:,.3f} kg")
        line = ""
        x = margin + 10
        y_line = y_cursor
        for token in numbers:
            try_w = c.stringWidth(line + ("" if not line else "   ") + token, "Helvetica", 9)
            if x + try_w > (W - margin):
                c.drawString(x, y_line, line)
                y_line -= 12
                line = token
            else:
                line = token if not line else f"{line}   {token}"
        if line:
            c.drawString(x, y_line, line)
            y_line -= 12
        y_cursor = y_line - 4
    else:
        c.drawString(margin + 10, y_cursor, "—")
        y_cursor -= 12

    # --- TOTALS (right) ---
    totals_x_right = W - margin
    totals_y = max(110, y_cursor - 6)

    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(totals_x_right - 85, totals_y, "Subtotal:")
    c.drawRightString(totals_x_right, totals_y, pkr_str(subtotal))

    if tax_rate and tax_amount > 0:
        c.setFont("Helvetica", 10)
        totals_y -= 14
        percent = (tax_rate * Decimal("100")).quantize(Decimal("0.01"))
        c.drawRightString(totals_x_right - 85, totals_y, f"{tax_label} ({percent}%):")
        c.drawRightString(totals_x_right, totals_y, pkr_str(tax_amount))

    c.setFont("Helvetica-Bold", 12)
    totals_y -= 18
    c.drawRightString(totals_x_right - 85, totals_y, "Grand Total:")
    c.drawRightString(totals_x_right, totals_y, pkr_str(grand_total))

    c.setFont("Helvetica", 10)
    totals_y -= 16
    c.drawRightString(totals_x_right - 85, totals_y, "Amount Paid:")
    c.drawRightString(totals_x_right, totals_y, pkr_str(amount_paid))

    c.setFont("Helvetica-Bold", 12)
    totals_y -= 18
    c.drawRightString(totals_x_right - 85, totals_y, "Balance Due:")
    c.drawRightString(totals_x_right, totals_y, pkr_str(balance_due))

    # Material balances (left)
    c.setFont("Helvetica", 9)
    c.drawString(margin, totals_y, f"Polyethylene Bags Balance: {before_balance:,.3f} kg")

    # Payments mini list
    footer_y = 60
    py_y = max(footer_y + 14, totals_y - 20)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, py_y, "Payments:")
    py_y -= 14
    c.setFont("Helvetica-Oblique", 9)

    if allocations:
        for a in allocations:
            p = a.payment
            date_str = getattr(p, "received_on", None)
            try:
                date_str = date_str.strftime("%Y-%m-%d")
            except Exception:
                date_str = str(date_str or "")
            row = f"{date_str}  ·  {(p.method or '')}  ·  {pkr_str(_as_decimal(a.amount))}"
            if getattr(p, "reference", ""):
                row += f"  ·  Ref: {p.reference}"
            c.drawString(margin + 10, py_y, row)
            py_y -= 12
    else:
        c.drawString(margin + 10, py_y, "—")
        py_y -= 12

    bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                  else getattr(settings, "BANK_DETAILS_LINES", []))
    _draw_footer(c, W, H, ss, bank_lines=bank_lines, user=user)

    c.save()
    return str(pdf_path)
################################################
## END INVOICE (SINGLE ORDER) PDF
################################################


# ====================
# Email sender (PDF)
# ====================
def send_invoice_email(order_id, pdf_path, to_email=None, subject=None, body=None):
    order = Order.objects.get(id=order_id)
    pdf_path = str(Path(pdf_path))

    recipient = to_email or (order.customer.email or "").strip()
    if not recipient:
        raise ValueError("Customer has no email address. Please add an email to the Customer record.")

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or getattr(settings, "EMAIL_HOST_USER", None)
    if not from_email:
        raise ValueError("Email sender not configured. Set DEFAULT_FROM_EMAIL or EMAIL_HOST_USER in settings.py.")

    inv_no = order.invoice_number or f"INV{order.id}"
    subject = subject or f"Invoice {inv_no}"
    customer_name = order.customer.contact_name or order.customer.company_name or "Customer"
    company_name = getattr(settings, "COMPANY_NAME", "S.S Plastic")
    body = body or (
        f"Dear {customer_name},\n\n"
        f"Please find attached your invoice {inv_no}.\n\n"
        f"Thank you,\n{company_name}"
    )

    from django.core.mail import EmailMessage
    email = EmailMessage(subject=subject, body=body, from_email=from_email, to=[recipient])
    email.attach_file(pdf_path)
    email.send(fail_silently=False)
    return True


################################################
## Customer Statement PDF (Ledgers)
################################################

def generate_customer_ledger_pdf(customer_id, start_date, end_date, user=None) -> str:
    """
    Customer material ledger: challan-book style table with the same
    header, footer and TableStyle as the statement PDF.
    Columns:
      Date | DC # | Size | Micron | Treatment | Receipt | Issued | Customer
    """
    customer = Customer.objects.get(id=customer_id)
    ss = get_site_settings()

    # --- Query entries ---
    entries = (
    CustomerMaterialLedger.objects
    .filter(customer=customer, date__range=(start_date, end_date))
    .select_related("order", "receipt")
    .order_by("order__delivery_challan", "date", "id")
)

    # --- Build rows ---
    header = ["Date", "DC #", "Size", "Micron", "Treatment", "Receipt", "Issued", "Customer"]
    rows = [header]

    tot_in  = Decimal("0.000")
    tot_out = Decimal("0.000")

    for e in entries:
        # Defaults
        dc = ""
        size = ""
        micron = ""
        treat = ""
        recv = ""
        issued = ""

        # If OUT row is tied to an order, extract challan details
        if e.type == "OUT" and e.order_id:
            o = e.order
            dc     = str(getattr(o, "delivery_challan", "") or "")
            size   = str(getattr(o, "roll_size", "") or "")
            # Pick your available fields for micron/treatment; adjust if your model differs
            micron = str(getattr(o, "micron", "") or getattr(o, "micron_from", "") or "")
            treat  = str(getattr(o, "current_type", "") or getattr(o, "current_type", "") or "")
            issued_qty = dkg(Decimal(-(e.delta_kg or 0)))  # OUT is negative
            issued = f"{issued_qty:,.3f}"
            tot_out += issued_qty
        elif e.type == "IN":
            recv_qty = dkg(Decimal(e.delta_kg or 0))
            recv = f"{recv_qty:,.3f}"
            tot_in += recv_qty

        rows.append([
            e.date.date().isoformat(),
            dc,
            size,
            micron,
            treat,
            recv,
            issued,
            customer.company_name,  # keep column for consistent look, same customer per sheet
        ])

    # Totals row
    rows.append([
        "", "", "", "", "Total",
        f"{dkg(tot_in):,.3f}",
        f"{dkg(tot_out):,.3f}",
        "",
    ])

    # --- PDF path ---
    safe_name = "".join(ch for ch in customer.company_name if ch.isalnum() or ch in (" ", "_", "-")).strip()
    outdir = Path(getattr(settings, "INVOICE_OUTPUT_DIR", "invoices"))
    outdir.mkdir(parents=True, exist_ok=True)
    pdf_path = outdir / f"Ledger-{safe_name}-{start_date}_to_{end_date}.pdf"

    # --- Canvas ---
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    W, H = A4
    margin = 15 * mm
    right_x = W - margin
    top_y = H - margin

    # --- Logo + header (same logic as statement) ---
    logo_path = None
    if ss and getattr(ss, "logo", None):
        try:
            logo_path = ss.logo.path
        except Exception:
            logo_path = None
    if not logo_path:
        lp = getattr(settings, "INVOICE_LOGO_PATH", "")
        logo_path = str(lp) if lp else None

    logo_w, logo_h = (38 * mm, 18 * mm)
    if logo_path:
        try:
            c.drawImage(
                ImageReader(logo_path),
                margin, top_y - logo_h, width=logo_w, height=logo_h,
                preserveAspectRatio=True, mask="auto"
            )
        except Exception:
            pass

    # Company text on the right (reuse your helper if you prefer)
    company_name = (ss.company_name if ss and ss.company_name else getattr(settings, "COMPANY_NAME", "Your Company"))
    company_addr_lines = (
        ss.company_address_list if ss and hasattr(ss, "company_address_list")
        else getattr(settings, "COMPANY_ADDRESS_LINES", [])
    )
    _draw_multiline_right(c, [company_name] + list(_as_lines(company_addr_lines)),
                          right_x, top_y, leading=14, font="Helvetica", size=10)

    # Title
    c.setTitle(f"Ledger-{customer.company_name}-{start_date}_to_{end_date}")
    title_y = top_y - (logo_h + 35 if logo_path else 55)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(W/2, title_y, f"{customer.company_name}")

    c.setFont("Helvetica", 14)
    c.drawCentredString(
    W / 2,
    title_y - 22,
    f"Ledger Records ({start_date.strftime('%d %B %Y')} → {end_date.strftime('%d %B %Y')})"
    )

    # Draw table
    y = (title_y - 50)  # start below the subtitle

    # Column widths (tuned for A4, similar density to your statement tables)
    base_cols = [85, 45, 50, 55, 70, 75, 75, 120]  # Date, DC, Size, Micron, Treat, Recv, Issued, Customer
    avail = W - 2 * margin
    scale = min(1.0, avail / float(sum(base_cols)))
    col_widths = [w * scale for w in base_cols]

    t = Table(rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(style)  # <<< reuse your global TableStyle

    tw, th = t.wrapOn(c, W, H)
    x_center = (W - tw) / 2.0

    # page break safety (same as statement)
    if y - th < 90:
        bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                      else getattr(settings, "BANK_DETAILS_LINES", []))
        _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                     ref=f"Ledger-{customer.company_name}-{start_date}_to_{end_date}", user=user)
        c.showPage()
        _draw_page_header(c, W, H, ss)
        y = H - margin

    t.drawOn(c, x_center, y - th)
    y = y - th - 20

    # Optional: notes block (same as statement)
    if ss and (getattr(ss, "notes", "") or getattr(ss, "notes_list", None)):
        y = _draw_notes_box(c, W, H, ss, y)

    # Footer (same as statement)
    bank_lines = (ss.bank_details_list if (ss and hasattr(ss, "bank_details_list"))
                  else getattr(settings, "BANK_DETAILS_LINES", []))
    _draw_footer(c, W, H, ss, bank_lines=bank_lines,
                 ref=f"Ledger-{customer.company_name}-{start_date}_to_{end_date}", user=user)

    c.save()
    return str(pdf_path)

################################################
## END Customer Statement PDF (Ledgers)
################################################