from datetime import date, timedelta
from django.contrib import admin, messages
from django.urls import path, reverse
from django import forms
from django.core.exceptions import ValidationError
from django.http import FileResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.utils.html import format_html
from django.shortcuts import redirect, get_object_or_404, render
from django.db.models import Sum, F, Value, ExpressionWrapper, DecimalField, Q,F, IntegerField
from django.db.models.functions import Coalesce
from django.utils.timezone import now
from django.utils import timezone
from pathlib import Path
from decimal import Decimal
from calendar import month_name, monthrange
from core.models.common import money_int_pk
from core.models.raw_material import BAG_WEIGHT_KG, RawMaterialPurchasePayment

# ✅ remove the bad absolute import; use relative imports only
from core.models.settings import SiteSettings
from core.services.material_sync import sync_order_material_ledger
from core.models import (
    Customer, Order, OrderItem, OrderRoll,
    MaterialReceipt, CustomerMaterialLedger,
    ExpenseCategory, Expense, SalaryPayment,
    Employee, Attendance
)
from core.models.raw_material import RawMaterialTxn, SupplierPayment, RawMaterialPurchasePayment
from core.services.purchase_pdf import generate_rm_purchase_statement
from core.services.signals_billing import propagate_carry_forward
from core.utils_billing import compute_customer_balance_as_of
from core.utils_money import to_rupees_int
from core.utils_weight import D, dkg

# ✅ Payment/Allocation live in models_ar, import them from there (not from .models)
from .models_ar import FINAL_STATES, Payment, PaymentAllocation
from .utils import generate_invoice, send_invoice_email, generate_customer_monthly_statement

from django.db.models import Sum
from django.utils.safestring import mark_safe

KG = DecimalField(max_digits=12, decimal_places=3)
MONEY = DecimalField(max_digits=18, decimal_places=2)
STATUS_STYLES = {
    "DRAFT":     {"bg": "#e2e8f0", "fg": "#111827", "bd": "#cbd5e1"},  # slate
    "CONFIRMED": {"bg": "#dbeafe", "fg": "#1e3a8a", "bd": "#bfdbfe"},  # blue
    "INPROD":    {"bg": "#fef3c7", "fg": "#92400e", "bd": "#fde68a"},  # amber
    "READY":     {"bg": "#dcfce7", "fg": "#065f46", "bd": "#bbf7d0"},  # green
    "DELIVERED": {"bg": "#d1fae5", "fg": "#065f46", "bd": "#a7f3d0"},  # teal/green
    "CLOSED":    {"bg": "#d1fae5", "fg": "#064e3b", "bd": "#6ee7b7"},  # emerald
    "CANCELLED": {"bg": "#fee2e2", "fg": "#7f1d1d", "bd": "#fecaca"},  # red
}

def _available_material_kg(customer):
    """
    Returns Decimal kg balance from the ledger for the given customer.
    """
    agg = (
        CustomerMaterialLedger.objects
        .filter(customer=customer)
        .aggregate(
            b=Coalesce(Sum('delta_kg'), Value(Decimal('0'), output_field=KG))
        )
    )
    return agg['b'] or Decimal('0')

class PaymentSelect(forms.Select):
    """
    Select widget that can disable options and style them (strike-through).
    We pass `disabled_ids` when constructing the widget.
    """
    def __init__(self, *args, disabled_ids=None, **kwargs):
        self.disabled_ids = set(disabled_ids or [])
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        try:
            pk = int(option.get("value"))
        except (TypeError, ValueError):
            pk = None
        if pk in self.disabled_ids:
            option.setdefault("attrs", {})
            option["attrs"]["disabled"] = True
            option["attrs"]["style"] = "text-decoration: line-through; color:#9ca3af;"
            # Optional: make it visually clear
            option["label"] = f"{option['label']} (fully allocated)"
        return option

class PaymentAllocationForOrderForm(forms.ModelForm):
    class Meta:
        model = PaymentAllocation
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        # parent order object is injected by the inline (below)
        order_obj = kwargs.pop("order_obj", None)
        super().__init__(*args, **kwargs)

        field = self.fields["payment"]

        if not order_obj or not getattr(order_obj, "customer_id", None):
            # No parent order yet → keep empty; tell the user to save first.
            field.queryset = Payment.objects.none()
            field.help_text = "Save the order first to choose customer payments."
            return

        money = DecimalField(max_digits=18, decimal_places=2)

        # Annotate each Payment with allocated sum so we can filter/label efficiently.
        qs = (
            Payment.objects
            .filter(customer=order_obj.customer)
            .annotate(
                allocated=Coalesce(Sum("allocations__amount"), Value(Decimal("0.00"), output_field=money))
            )
            .order_by("-received_on", "-id")
        )

        # Build choices + disabled list
        choices = []
        disabled_ids = []
        for p in qs:
            unapplied = (p.amount or Decimal("0.00")) - (getattr(p, "allocated", Decimal("0.00")) or Decimal("0.00"))
            label = f"Pmt #{p.id} — Unapplied ${unapplied:,.2f} / Total ${p.amount:,.2f} [{p.method}] on {p.received_on}"
            choices.append((p.pk, label))
            if unapplied <= 0:
                disabled_ids.append(p.pk)

        field.queryset = qs  # validation against real rows
        field.widget = PaymentSelect(disabled_ids=disabled_ids)
        field.choices = choices
        field.help_text = "Pick a payment with remaining unapplied balance."

class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    fields = ("order", "amount", "applied_on",
              "order_outstanding_now", "payment_unapplied_now")  # + new
    readonly_fields = ("order_outstanding_now", "payment_unapplied_now")
    autocomplete_fields = ("order",)

    def order_outstanding_now(self, obj):
        if not obj or not obj.pk:
            return "—"
        # uses your Order.outstanding_balance property
        return f"{obj.order.outstanding_balance:,.2f}"
    order_outstanding_now.short_description = "Order Outstanding (now)"

    def payment_unapplied_now(self, obj):
        if not obj or not obj.pk:
            return "—"
        # uses Payment.unapplied_amount property
        return f"{obj.payment.unapplied_amount:,.2f}"
    payment_unapplied_now.short_description = "Payment Unapplied (now)"
    def has_add_permission(self, request, obj=None):
        return False
# in core/admin.py

@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "customer_name", "payment", "order",
        "amount", "order_outstanding_now", "payment_unapplied_now", "applied_on"
    )
    list_filter  = ("applied_on", "order__status", "order__customer")
    search_fields = ("order__invoice_number", "payment__reference", "order__customer__company_name")
    ordering = ("-applied_on", "-id")
    list_per_page = 50

    def customer_name(self, obj):
        return getattr(obj.order.customer, "company_name", "")
    customer_name.short_description = "Customer"

    def order_outstanding_now(self, obj):
        return f"{obj.order.outstanding_balance:,.2f}"
    order_outstanding_now.short_description = "Order Outstanding (now)"

    def payment_unapplied_now(self, obj):
        return f"{obj.payment.unapplied_amount:,.2f}"
    payment_unapplied_now.short_description = "Payment Unapplied (now)"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "received_on", "method", "reference",
                    "amount_display", "allocated_amount_display", "unapplied_amount_display")
    list_filter = ("method", "received_on", "customer")
    search_fields = ("reference", "customer__company_name")
    inlines = [PaymentAllocationInline]
    readonly_fields = ()
    actions = ["auto_apply_selected"]
    ordering = ("-received_on", "-id")
    list_per_page = 50

    @admin.display(description="Amount (PKR)")
    def amount_display(self, obj):
        # Shows 0 once payments ≥ initial carry
        return money_int_pk(obj.amount)
    
    @admin.display(description="Allocated (PKR)")
    def allocated_amount_display(self, obj):
        # Shows 0 once payments ≥ initial carry
        return money_int_pk(obj.allocated_amount)
    
    @admin.display(description="Leftover (PKR)")
    def unapplied_amount_display(self, obj):
        # Shows 0 once payments ≥ initial carry
        return money_int_pk(obj.unapplied_amount)
    
    @admin.display(description="Leftover (PKR)")
    def unapplied_amount_display(self, obj):
        # prefer stored field; fallback to live compute
        val = getattr(obj, "unapplied_amount", None)
        if val is None:
            val = obj.unapplied_amount
        try:
            n = int(val or 0)
        except (TypeError, ValueError):
            n = 0

        if n >= 0:
            return f"PKR {n:,}"
        # overpaid → show positive with credit label
        return f"PKR {abs(n):,} (credit)"

class OrderAllocationInlineReadonly(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    can_delete = False
    # show helpful columns
    fields = ("payment_link", "amount", "applied_on", "order_outstanding_now", "payment_unapplied_now")
    readonly_fields = ("payment_link", "amount", "applied_on", "order_outstanding_now", "payment_unapplied_now")

    def has_add_permission(self, request, obj=None):
        # no blank form rows -> avoids "this field is required"
        return False

    # pretty link to the payment
    def payment_link(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("admin:core_payment_change", args=[obj.payment_id])
        return format_html('<a href="{}">Pmt #{}</a>', url, obj.payment_id)
    payment_link.short_description = "Payment"

    # the two convenience “remaining” columns you already added
    def order_outstanding_now(self, obj):
        return f"{obj.order.outstanding_balance:,.2f}"
    order_outstanding_now.short_description = "Order Outstanding (now)"

    def payment_unapplied_now(self, obj):
        return f"{obj.payment.unapplied_amount:,.2f}"
    payment_unapplied_now.short_description = "Payment Unapplied (now)"


## NOT IN USE ANYMORE
class OrderAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    fields = ("payment", "amount", "applied_on")
    # Remove autocomplete so our custom widget/labels show up
    # autocomplete_fields = ("payment",)

    # show the two convenience columns you added earlier (optional)
    readonly_fields = ()
    ordering = ("-applied_on", "-id")

    def get_formset(self, request, obj=None, **kwargs):
        """
        Inject the parent order (obj) into the form so we can scope payments to the same customer
        and compute unapplied/total labels.
        """
        parent_order = obj

        class _Form(PaymentAllocationForOrderForm):
            def __init__(self2, *args, **kw):
                kw["order_obj"] = parent_order
                super().__init__(*args, **kw)

        kwargs["form"] = _Form
        return super().get_formset(request, obj, **kwargs)
#### --------------------------------------
#### END Accounts Recievable Functionality
#### --------------------------------------
class CustomerPaymentInline(admin.TabularInline):
    model = Payment   # from models_ar
    extra = 0
    fields = ('received_on', 'amount', 'method', 'reference', 'notes')
    ordering = ('-received_on',)

# ---------- Admin ----------
@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    change_form_template = "core/admin/core/customer/change_form.html"

    list_display = (
        "company_name",
        "contact_name",
        "material_balance_display",
        "carry_forward_display",
        "pending_display",
        "lifetime_in_display",
    )
    readonly_fields = (
        "material_balance_display",
        "pending_display",
        "lifetime_in_display",
    )
    fieldsets = (
        ("Personal Details", {"fields": ("company_name", "contact_name")}),
        ("Company Details", {"fields": ("country", "phone", "email", "address")}),
        (
            "Customer Account Stats",
            {
                "fields": (
                    "previous_pending_balance_pkr",
                    "material_balance_display",
                    "lifetime_in_display",
                    "pending_display",
                )
            },
        ),
        ("Negative Dana Charge (Override)", {
            "fields": ("charge_negative_dana", "negative_dana_rate_pkr"),
        }),
    )
    search_fields = ("company_name", "contact_name", "phone", "email")
    ordering = ("company_name",)
     # ---------- Helpers ----------
    def _compute_range(self, request):
        """
        Returns (start_date, end_date, preset, year, month) using:
        - preset=month -> uses year/month selects
        - 30/60/90/120 -> last N days (inclusive, ending today)
        - year -> Jan 1 ... Dec 31 of selected year
        - custom -> uses start/end (YYYY-MM-DD)
        """
        today = timezone.localdate()
        preset = (request.GET.get("preset") or "month").lower()

        # defaults from today (still returned for consistency)
        y = int(request.GET.get("year") or today.year)
        m = int(request.GET.get("month") or today.month)

        if preset == "custom":
            start_str = request.GET.get("start")
            end_str   = request.GET.get("end")
            try:
                start = date.fromisoformat(start_str) if start_str else None
                end   = date.fromisoformat(end_str) if end_str else None
            except Exception:
                start = end = None
            if not start or not end:
                raise ValueError("Invalid period preset: custom requires 'start' and 'end' (YYYY-MM-DD).")
            if start > end:
                start, end = end, start
            return start, end, preset, y, m

        if preset == "month":
            start = date(y, m, 1)
            end = date(y, m, monthrange(y, m)[1])
            return start, end, preset, y, m

        if preset == "year":
            start = date(y, 1, 1)
            end = date(y, 12, 31)
            return start, end, preset, y, m

        if preset in {"30", "60", "90", "120"}:
            days = int(preset)
            end = today
            # inclusive: e.g. "30" => last 30 days incl. today
            start = today - timedelta(days=days - 1)
            return start, end, preset, y, m

        raise ValueError("Invalid period preset")
    
    # ---------- URLs ----------
    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            # Statement (same template as your billing PDF), but supports arbitrary ranges
            path(
                "<int:pk>/preview-range/",
                self.admin_site.admin_view(self.preview_statement_range),
                name="core_customer_preview_range",
            ),
            path(
                "<int:pk>/download-range/",
                self.admin_site.admin_view(self.download_statement_range),
                name="core_customer_download_range",
            ),
            # Printable ledger view for a range
            path(
                "<int:pk>/preview-ledger/",
                self.admin_site.admin_view(self.preview_ledger),
                name="core_customer_preview_ledger",
            ),

        ]
        return my_urls + urls
    
    #Live Balance
    @property
    def pending_balance_live_pkr(self):
        """Live balance using the same math as billing PDFs."""
        return compute_customer_balance_as_of(self, timezone.localdate())
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            # Σ ledger deltas (IN − OUT) per customer
            mat_balance_calc=Coalesce(
                Sum("material_ledger__delta_kg", output_field=KG),
                Value(Decimal("0.000"), output_field=KG),
            )
        )


    @admin.display(description="Material Balance (kg)")
    def material_balance_display(self, obj):
        val = getattr(obj, "mat_balance_calc", Decimal("0.000")) or Decimal("0.000")
        return f"{val.quantize(Decimal('0.001')):,.3f}"


    @admin.display(description="Total IN (Lifetime kg)")
    def lifetime_in_display(self, obj):
        agg = obj.material_ledger.filter(delta_kg__gt=0).aggregate(
            s=Coalesce(Sum("delta_kg", output_field=KG), Value(Decimal("0.000"), output_field=KG))
        )
        val = agg["s"] or Decimal("0.000")
        return f"{val.quantize(Decimal('0.001')):,.3f}"

    @admin.display(description="Carry-Forward (PKR)")
    def carry_forward_display(self, obj):
        from core.models.common import money_int_pk
        return money_int_pk(obj.carry_remaining_pkr)

    @admin.display(description="Pending / Credit (PKR)")
    def pending_display(self, obj):
        n = compute_customer_balance_as_of(obj)  # always live
        return f"PKR {n:,}" if n >= 0 else f"PKR {abs(n):,} (credit)"
    
    

    # ----- Statement preview/download helpers -----
    def _parse_period(self, request):
        """Get (year, month) from GET with safe defaults and validation."""
        today = now().date()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
        except (TypeError, ValueError):
            return None, None
        if not (2000 <= year <= 2100) or not (1 <= month <= 12):
            return None, None
        return year, month

    def download_statement_range(self, request, pk):
        try:
            start, end, preset, y, m = self._compute_range(request)
        except Exception as e:
            return HttpResponseBadRequest(str(e))

        try:
            from core.utils import generate_customer_statement_range
        except Exception:
            from core.utils import generate_customer_monthly_statement as _monthly
            if preset != "month":
                return HttpResponseBadRequest("Range generator missing. Please add generate_customer_statement_range.")
            pdf_path = _monthly(pk, y, m, user=request.user)
        else:
            pdf_path = generate_customer_statement_range(pk, start, end, user=request.user)

        fname = Path(pdf_path).name
        return FileResponse(
            open(pdf_path, "rb"),
            as_attachment=True,
            filename=fname,
            content_type="application/pdf",
        )

    # ---------- Range handlers ----------
    def preview_statement_range(self, request, pk):
        try:
            start, end, preset, y, m = self._compute_range(request)
        except Exception as e:
            return HttpResponseBadRequest(str(e))

        # Your range-capable generator (see note below)
        try:
            from core.utils import generate_customer_statement_range
        except Exception:
            # Fallback to monthly if you haven't created the range version yet
            from core.utils import generate_customer_monthly_statement as _monthly
            # emulate by calling monthly if preset == month, else bail
            if preset != "month":
                return HttpResponseBadRequest("Range generator missing. Please add generate_customer_statement_range.")
            pdf_path = _monthly(pk, y, m, user=request.user)
        else:
            pdf_path = generate_customer_statement_range(pk, start, end, user=request.user)

        fname = Path(pdf_path).name
        resp = FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp
    
    def preview_ledger(self, request, pk):
        # Use the unified parser (supports month/year/rolling/custom)
        try:
            start_date, end_date, _preset, _y, _m = self._compute_range(request)
        except Exception:
            # Safe fallback = current month
            today = timezone.localdate()
            start_date = date(today.year, today.month, 1)
            end_date = date(today.year, today.month, monthrange(today.year, today.month)[1])

        from core.utils import generate_customer_ledger_pdf
        pdf_path = generate_customer_ledger_pdf(pk, start_date, end_date, user=request.user)

        fname = Path(pdf_path).name
        resp = FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp

    # ---------- Change form extras (adds the Period dropdown + buttons) ----------
    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}

        today = now().date()
        months = [(i, month_name[i]) for i in range(1, 13)]
        years = list(range(today.year - 5, today.year + 2))

        default_month = int(request.GET.get("month", today.month))
        default_year = int(request.GET.get("year", today.year))
        default_preset = request.GET.get("preset", "month")

        # URLs for the buttons
        preview_range_url = reverse("admin:core_customer_preview_range", args=[object_id])
        download_range_url = reverse("admin:core_customer_download_range", args=[object_id])
        ledger_preview_url = reverse("admin:core_customer_preview_ledger", args=[object_id])

        # existing quick links
        cash_ledger_url = reverse("admin:core_payment_changelist") + f"?customer__id__exact={object_id}"
        material_ledger_url = reverse("admin:core_customermaterialledger_changelist") + f"?customer__id__exact={object_id}"
        allocation_ledger_url = reverse("admin:core_paymentallocation_changelist") + f"?order__customer__id__exact={object_id}"

        extra_context.update(
            {
                "months": months,
                "years": years,
                "default_month": default_month,
                "default_year": default_year,
                "default_preset": default_preset,
                "statement_preview_range_url": preview_range_url,
                "statement_download_range_url": download_range_url,
                "ledger_preview_url": ledger_preview_url,
                "cash_ledger_url": cash_ledger_url,
                "material_ledger_url": material_ledger_url,
                "allocation_ledger_url": allocation_ledger_url,
            }
        )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

class PurchasePaymentInline(admin.TabularInline):
    model = RawMaterialPurchasePayment
    extra = 1
    autocomplete_fields = ["payment"]  # if you enabled autocomplete for Payment
    fields = ("payment","note")
    
class RawMaterialTxnAdminForm(forms.ModelForm):
    class Meta:
        model = RawMaterialTxn
        fields = [
            # exactly what you asked for:
            "material_type",     # FILM/Tape
            "when",              # Date of Purchase
            "rate_pkr",          # Rate of Purchase
            "bags_count",        # QTY Purchase (bags)
            "supplier_name",     # Supplier Name
            "dc_number",         # DC #
            "kind",              # keep kind for SALE vs PURCHASE
            "to_customer",       # SALE: select customer; PURCHASE: hidden
            "memo",
            # hidden/auto: qty_kg, amount_pkr, from_customer, created_by
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        kind = (
            self.data.get("kind")
            or getattr(self.instance, "kind", None)
            or RawMaterialTxn.Kind.PURCHASE
        )

        # Labels to match your wording
        self.fields["when"].label = "Date of Purchase"
        self.fields["rate_pkr"].label = "Rate of Purchase (PKR/kg)"
        self.fields["bags_count"].label = "QTY Purchase (bags)"
        self.fields["dc_number"].label = "DC #"
        self.fields["supplier_name"].label = "Supplier Name"

        # Requireds
        self.fields["material_type"].required = True
        self.fields["when"].required = True
        self.fields["rate_pkr"].required = (kind in (RawMaterialTxn.Kind.PURCHASE, RawMaterialTxn.Kind.SALE))
        self.fields["bags_count"].required = True if kind == RawMaterialTxn.Kind.PURCHASE else False
        self.fields["supplier_name"].required = (kind == RawMaterialTxn.Kind.PURCHASE)

        # PURCHASE: we don't let user pick company stock
        if kind == RawMaterialTxn.Kind.PURCHASE:
            self.fields["to_customer"].widget = forms.HiddenInput()

class PurchasePaymentInline(admin.TabularInline):
    model = RawMaterialPurchasePayment
    extra = 1
    autocomplete_fields = ("payment",)

@admin.register(RawMaterialTxn)
class RawMaterialTxnAdmin(admin.ModelAdmin):
    list_display = ("when","kind","supplier_name","from_customer","to_customer","qty_kg","rate_display","Total_Amount","supplier_due_display","dc_number")
    list_filter  = ("kind","when","material_type")
    search_fields = ("supplier_name","dc_number","memo","to_customer__company_name","from_customer__company_name")
    autocomplete_fields = ("from_customer","to_customer")
    inlines = (PurchasePaymentInline,)
    
    # Keep your server-side safety (apply() calculates and writes ledger)
    def save_model(self, request, obj, form, change):
        obj.apply(user=request.user)
        form.save_m2m()
    
    @admin.display(description="Rate")
    def rate_display(self, obj):
        return Decimal(obj.rate_pkr)    
    @admin.display(description="Outstanding (PKR)")
    def supplier_due_display(self, obj):
        if obj.kind != RawMaterialTxn.Kind.PURCHASE:
            return "—"
        return money_int_pk(obj.supplier_outstanding_pkr)
    @admin.display(description="Total Payment Due (PKR)")
    def Total_Amount(self, obj):
        if obj.kind != RawMaterialTxn.Kind.PURCHASE:
            return "—"
        # uses the property we added: amount_pkr - linked supplier payments (never negative)
        return money_int_pk(obj.amount_pkr)
    
    # 1) Add the readonly field only for PURCHASE rows
    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj and obj.kind == RawMaterialTxn.Kind.PURCHASE:
            ro.append("purchase_pdf_actions")
        return ro

    # # 2) Inject an extra fieldset only for PURCHASE rows
    # def get_fieldsets(self, request, obj=None):
    #     fs = list(super().get_fieldsets(request, obj))
    #     if obj and obj.kind == RawMaterialTxn.Kind.PURCHASE:
    #         fs.append(("Purchase Statement", {"fields": ("purchase_pdf_actions",)}))
    #     return fs

    # 3) Render the two buttons
    @admin.display(description="Purchase PDF")
    def purchase_pdf_actions(self, obj):
        if not obj or obj.kind != RawMaterialTxn.Kind.PURCHASE:
            return "—"
        prev = reverse("admin:core_rawmaterialtxn_preview_pdf", args=[obj.pk])
        down = reverse("admin:core_rawmaterialtxn_download_pdf", args=[obj.pk])
        return format_html(
            '<a class="button" target="_blank" href="{}">Preview PDF</a>&nbsp;'
            '<a class="button" href="{}">Download PDF</a>',
            prev, down
        )

    # 4) Admin URLs for the views
    def get_urls(self):
        urls = super().get_urls()
        my = [
            path(
                "<int:pk>/preview-purchase/",
                self.admin_site.admin_view(self.preview_purchase_pdf),
                name="core_rawmaterialtxn_preview_pdf",
            ),
            path(
                "<int:pk>/download-purchase/",
                self.admin_site.admin_view(self.download_purchase_pdf),
                name="core_rawmaterialtxn_download_pdf",
            ),
        ]
        return my + urls

    # 5) Views that build/serve the PDF
    def preview_purchase_pdf(self, request, pk):
        try:
            path = generate_rm_purchase_statement(pk, user=request.user)
        except Exception as e:
            return HttpResponseBadRequest(str(e))
        fname = Path(path).name
        resp = FileResponse(open(path, "rb"), content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp

    def download_purchase_pdf(self, request, pk):
        try:
            path = generate_rm_purchase_statement(pk, user=request.user)
        except Exception as e:
            return HttpResponseBadRequest(str(e))
        return FileResponse(open(path, "rb"), as_attachment=True,
                           filename=Path(path).name, content_type="application/pdf")
         
    class Media:
        # put the JS below at: core/static/core/admin/raw_material_txn.js
        js = ("core/admin/raw_material_txn.js",)

    # Compute and write ledgers through the model API
    def save_model(self, request, obj, form, change):
        # For PURCHASE, derive qty again here for safety (matches model.clean)
        if obj.kind == RawMaterialTxn.Kind.PURCHASE:
            obj.qty_kg = dkg(Decimal(obj.bags_count or 0) * BAG_WEIGHT_KG)
        # Amount is always auto for PURCHASE/SALE; model.clean also enforces it
        obj.apply(user=request.user)  # does clean(), computes, saves, and writes ledger rows
        form.save_m2m()

@admin.register(SupplierPayment)
class SupplierPaymentAdmin(admin.ModelAdmin):
    list_display = ("paid_on","supplier_name","method","bank","reference","amount_pkr","notes")
    list_filter  = ("method","bank","paid_on")
    search_fields = ("supplier_name","reference","notes")

class PaymentStatusFilter(admin.SimpleListFilter):
    title = 'Payment Status'
    parameter_name = 'payment_status'

    def lookups(self, request, model_admin):
        return [
            ('full', 'Fully Paid'),
            ('partial', 'Partially Paid'),
            ('pending', 'Pending'),
        ]

    def queryset(self, request, queryset):
        # queryset here is already annotated via OrderAdmin.get_queryset()
        val = self.value()
        if val == 'full':
            return queryset.filter(total_amount_calc__lte=F('total_paid_calc'))
        elif val == 'partial':
            return queryset.filter(Q(total_paid_calc__gt=0) & Q(total_paid_calc__lt=F('total_amount_calc')))
        elif val == 'pending':
            return queryset.filter(total_paid_calc=0)
        return queryset
# -------------------------
#  MATERIAL
# -------------------------

@admin.register(MaterialReceipt)
class MaterialReceiptAdmin(admin.ModelAdmin):
    list_display = ('customer', 'date', 'bags_count', 'extra_kg', 'total_kg', 'notes')
    list_filter = ('customer', 'date')
    search_fields = ('customer__company_name', 'notes')
    date_hierarchy = 'date'
    ordering = ('-date', '-id')
    list_per_page = 50

@admin.register(CustomerMaterialLedger)
class CustomerMaterialLedgerAdmin(admin.ModelAdmin):
    list_display  = ('customer', 'date', 'type', 'delta_kg', 'material_type', 'order', 'receipt', 'memo')
    list_filter   = ('type', 'customer', 'date', 'material_type')
    search_fields = ('customer__company_name', 'memo')
    date_hierarchy = 'date'
    ordering = ('-date', '-id')
    list_per_page = 50


# -------------------------
# ORDER
# -------------------------

class OrderItemInlineFormSet(forms.BaseInlineFormSet):
    """
    Validates that the customer has enough raw material for the NEW total of this order
    using the inline values submitted in this request (no second save/reopen needed).
    """
    def clean(self):
        super().clean()
        if any(self.errors):
            return

        order = self.instance
        customer = getattr(order, 'customer', None)
        if not customer:
            return  # main form will require a customer

        # 1) Proposed total kg from the posted rows (ignore DELETE)
        proposed_total_kg = Decimal('0')
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            cd = form.cleaned_data
            if cd.get("DELETE"):
                continue
            w = cd.get("roll_weight") or Decimal('0')
            q = cd.get("quantity") or 0
            w = w if isinstance(w, Decimal) else Decimal(str(w))
            q = Decimal(str(q))
            proposed_total_kg += (w * q)

        # 2) Previously saved total kg for this order (DB)
        previous_total_kg = Decimal('0')
        if order.pk:
            kg_expr = ExpressionWrapper(
                Coalesce(F('roll_weight'), Value(Decimal('0'))) *
                Coalesce(F('quantity'), Value(0)),
                output_field=KG
            )
            previous_total_kg = (
                OrderItem.objects
                .filter(order=order)
                .aggregate(total=Coalesce(Sum(kg_expr), Value(Decimal('0'), output_field=KG)))
            )['total'] or Decimal('0')

        # 3) Net change requested now
        delta_kg = proposed_total_kg - previous_total_kg

        # 4) Available material (Decimal)
        avail_kg = _available_material_kg(customer)

        if delta_kg > 0 and delta_kg > avail_kg:
            raise ValidationError(
                f"Not enough raw material to cover this change. "
                f"Additional needed: {delta_kg:.3f} kg, available: {avail_kg:.3f} kg."
            )

class OrderRollInline(admin.TabularInline):
    model = OrderRoll
    extra = 1
    fields = ("weight_kg", "barcode", "created_at")
    readonly_fields = ("created_at",)

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    formset = OrderItemInlineFormSet
    extra = 0
    fields = ("roll_weight", "quantity", "price_per_kg")

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    change_form_template = 'core/order_change_form.html'
    formset = OrderItemInlineFormSet,
    list_display = ("invoice_number","dcNumber","customer", "status", "grand_total_display", "total_allocated_display", "outstanding_balance_display",  "target_total_kg" ,"produced_kg")
    list_filter = ("status", "customer")
    search_fields = ("customer__company_name", "invoice_number")
    inlines = [OrderRollInline, OrderAllocationInlineReadonly]
    actions = ["include_gst_on", "include_gst_off"]
    fieldsets = (
        ("Customer & Terms", {"fields": ("customer", "payment_terms", "delivery_date")}),
         ("Status & Invoice", {"fields": ("status", "invoice_number", "delivery_challan", "delivery_challan_date")}),
        ("Order Details", {"fields": ("roll_size", "micron", "current_type",
                                      "target_total_kg", "price_per_kg", "tolerance_kg", "include_gst")}),
        
        
    )
    # @property
    def total_allocated_pkr(self) -> int:
        """
        Sum of allocations INCLUDING rounding write-off.
        """
        agg = self.payment_allocations.aggregate(
            s=Coalesce(
                Sum(F("amount") + F("rounding_pkr"), output_field=IntegerField()),
                0,
                output_field=IntegerField(),
            )
        )
        return int(agg["s"] or 0)
    # Pretty columns
    def subtotal_display(self, obj):
        return f"{obj.subtotal:,.2f}"
    subtotal_display.short_description = "Subtotal"

    def tax_display(self, obj):
        # Hide zero tax cleanly
        return "—" if obj.tax_amount == 0 else f"{obj.tax_amount:,.2f}"
    tax_display.short_description = "Tax"

    def grand_total_display(self, obj):
        return money_int_pk(obj.grand_total)
    grand_total_display.short_description = "Grand Total"

    def total_allocated_display(self, obj):
        return money_int_pk(obj.total_allocated)
    total_allocated_display.short_description = "Total Paid"

    # nice numbers
    def produced_kg_display(self, obj):
        return f"{obj.produced_kg:,.3f}"
    produced_kg_display.short_description = "Produced kg"

    def billable_kg_display(self, obj):
        return f"{obj.billable_kg:,.3f}"
    billable_kg_display.short_description = "Billable kg"

    def total_amount_display(self, obj):
        return f"{obj.total_amount:,.2f}"
    total_amount_display.short_description = "Total Amount"

    def total_paid_display(self, obj):
        return f"{obj.total_paid:,.2f}"
    total_paid_display.short_description = "Paid"

    @admin.display(description="DC #")
    def dcNumber(self, obj):
        return obj.delivery_challan
    
    

    def outstanding_balance_display(self, obj):
        return f"{obj.outstanding_balance:,.2f}"
    outstanding_balance_display.short_description = "Outstanding"

    @admin.display(description="Status", ordering="status")
    def colored_status(self, obj):
        code = (obj.status or "").upper()
        label = obj.get_status_display() if hasattr(obj, "get_status_display") else code
        s = STATUS_STYLES.get(code, {"bg": "#eee", "fg": "#111", "bd": "#ddd"})
        # pill style inline so you don't need extra CSS files
        return format_html(
            '<span style="display:inline-block;padding:.25rem .5rem;border-radius:999px;'
            'border:1px solid {bd};background:{bg};color:{fg};font-weight:600;'
            'font-size:12px;line-height:1;white-space:nowrap;">{label}</span>',
            bd=s["bd"], bg=s["bg"], fg=s["fg"], label=label
        )
    def generate_pdf(self, request, order_id, *args, **kwargs):
        """
        Build (or rebuild) the PDF and stream it to the browser.
        Add ?mode=inline to preview, default is download.
        """
        pdf_path = Path(generate_invoice(order_id, user=request.user))
        mode = request.GET.get('mode', 'download')
        return FileResponse(
            open(pdf_path, 'rb'),
            as_attachment=(mode == 'download'),
            filename=pdf_path.name,
            content_type='application/pdf'
        )
    def include_gst_on(self, request, queryset):
        updated = queryset.update(include_gst=True)
        self.message_user(request, f"Enabled GST on {updated} orders.")
    include_gst_on.short_description = "Enable GST on selected orders"

    def include_gst_off(self, request, queryset):
        updated = queryset.update(include_gst=False)
        self.message_user(request, f"Disabled GST on {updated} orders.")
    include_gst_off.short_description = "Disable GST on selected orders"
    
    def send_email(self, request, order_id, *args, **kwargs):
        """
        Generate the PDF (to be sure it’s fresh) and email it.
        Returns to the order change page with a success/error message.
        """
        try:
            pdf_path = Path(generate_invoice(order_id, user=request.user))
            send_invoice_email(order_id, pdf_path)
            messages.success(request, "Invoice emailed successfully.")
        except Exception as e:
            messages.error(request, f"Failed to send invoice email: {e}")
        # go back to the order page
        return HttpResponseRedirect(request.META.get('HTTP_REFERER') or '..')
    # END Generatae PDF and Email
    def save_related(self, request, form, formsets, change):
        # save inlines/items first so required_material_kg is accurate
        super().save_related(request, form, formsets, change)
        sync_order_material_ledger(form.instance)
    # Quick status actions in row (optional)
    def get_urls(self):
        urls = super().get_urls()
        my = [
            path("<int:order_id>/mark/<str:new_status>/", self.admin_site.admin_view(self.mark_status), name="order-mark-status"),
            path(
                '<int:order_id>/generate_pdf/',
                self.admin_site.admin_view(self.generate_pdf),
                name='core_order_generate_pdf'
            ),
            path(
                '<int:order_id>/send_email/',
                self.admin_site.admin_view(self.send_email),
                name='core_order_send_email'
            ),
        ]
        return my + urls
        
    def mark_status(self, request, order_id, new_status):
        order = self.get_object(request, order_id)
        if not order:
            self.message_user(request, "Order not found.", level=messages.ERROR)
            return redirect("admin:core_order_changelist")
        if new_status not in dict(Order.STATUS_CHOICES):
            self.message_user(request, "Invalid status.", level=messages.ERROR)
            return redirect("admin:core_order_change", object_id=order_id)
        order.status = new_status
        try:
            order.full_clean()
            order.save()
            self.message_user(request, f"Order {order.id} marked {new_status}.", level=messages.SUCCESS)
        except Exception as e:
            self.message_user(request, str(e), level=messages.ERROR)
        return redirect("admin:core_order_change", object_id=order_id)
    



# -------------------------
# END ORDER
# -------------------------

# -------------------------
# Expenses
# -------------------------

@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    search_fields = ('name',)
    list_display = ('name', 'description')

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'amount', 'expense_date', 'period')
    list_filter = ('category', 'period', 'expense_date')
    search_fields = ('title', 'notes')
    date_hierarchy = 'expense_date'

# -------------------------
# Employees
# -------------------------

class AttendanceInline(admin.TabularInline):
    model = Attendance
    extra = 0
    fields = ('date', 'status', 'hours_worked', 'notes')
    ordering = ('-date',)

class SalaryPaymentInline(admin.TabularInline):
    model = SalaryPayment
    extra = 0
    fields = ('period_month', 'period_year', 'gross_amount', 'paid_amount', 'payment_date', 'method', 'notes', 'slip')
    ordering = ('-period_year', '-period_month')

@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('name', 'phone', 'salary', 'join_date', 'contract_end_date', 'is_active')
    actions = ["duplicate_selected"]
    list_filter = ('is_active', 'join_date', 'contract_end_date')
    search_fields = ('name', 'cnic', 'phone', 'email')
    readonly_fields = ()
    fieldsets = (
        ('Identity', {
            'fields': ('name', 'cnic', 'phone', 'email', 'address'),
        }),
        ('Contract', {
            'fields': ('salary', 'join_date', 'hire_date', 'contract_end_date', 'is_active'),
        }),
        ('Files', {
            'fields': ('profile_picture', 'contract_file'),
        }),
    )
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:employee_id>/duplicate/",
                self.admin_site.admin_view(self.duplicate_employee),
                name="core_employee_duplicate",
            ),
        ]
        return custom_urls + urls
    def duplicate_employee(self, request, employee_id):
        employee = get_object_or_404(Employee, pk=employee_id)
        # Create a copy
        employee.pk = None
        employee.name = f"{employee.name} (Copy)"
        employee.save()
        self.message_user(request, f"Employee '{employee.name}' duplicated successfully.", messages.SUCCESS)
        return redirect("admin:core_employee_change", employee.pk)

    def duplicate_selected(self, request, queryset):
        for obj in queryset:
            obj.pk = None
            obj.name = f"{obj.name} (Copy)"
            obj.save()
        self.message_user(request, f"{queryset.count()} employee(s) duplicated successfully.", messages.SUCCESS)

    duplicate_selected.short_description = "Duplicate selected employees"
    inlines = [AttendanceInline, SalaryPaymentInline]

@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('employee', 'date', 'status', 'hours_worked', 'notes')
    list_filter = ('status', 'date', 'employee')
    search_fields = ('employee__name', 'notes')
    date_hierarchy = 'date'

@admin.register(SalaryPayment)
class SalaryPaymentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'period_month', 'period_year', 'gross_amount', 'paid_amount', 'outstanding', 'payment_date', 'method')
    list_filter = ('period_year', 'period_month', 'method', 'employee')
    search_fields = ('employee__name',)
    date_hierarchy = 'payment_date'
