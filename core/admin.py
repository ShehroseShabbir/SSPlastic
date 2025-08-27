from django.contrib import admin, messages
from django.urls import path, reverse
from django import forms
from django.core.exceptions import ValidationError
from django.http import FileResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.utils.html import format_html
from django.shortcuts import redirect, get_object_or_404, render
from django.db.models import Sum, F, Value, ExpressionWrapper, DecimalField, Q,F
from django.db.models.functions import Coalesce
from django.utils.timezone import now
from django.template.response import TemplateResponse
from pathlib import Path
from decimal import Decimal
from calendar import month_name
from django.db import transaction
from core.admin_forms.raw_material import PurchaseForm, SellForm, TransferForm
from core.models.common import money_int_pk
from core.models.raw_material import RawMaterialPurchasePayment

# ✅ remove the bad absolute import; use relative imports only
from .material_sync import sync_order_material_ledger
from core.models import (
    Customer, Order, OrderItem, OrderRoll,
    MaterialReceipt, CustomerMaterialLedger,
    ExpenseCategory, Expense, SalaryPayment,
    Employee, Attendance,RawMaterialTxn
)
# ✅ Payment/Allocation live in models_ar, import them from there (not from .models)
from .models_ar import Payment, PaymentAllocation
from .ar_utils import auto_apply_fifo
from .utils import generate_invoice, send_invoice_email, generate_customer_monthly_statement



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
                    "amount", "allocated_amount", "unapplied_amount")
    list_filter = ("method", "received_on", "customer")
    search_fields = ("reference", "customer__company_name")
    inlines = [PaymentAllocationInline]
    actions = ["auto_apply_selected"]
    ordering = ("-received_on", "-id")
    list_per_page = 50

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

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    change_form_template = "core/admin/core/customer/change_form.html"
    list_display = (
        "company_name",
        "contact_name",
        "lifetime_in_display",
        "total_material_display",
        "carry_forward_display",
        "total_due_with_carry_display",
    )
    readonly_fields = (
        "lifetime_in_display",
        "total_material_display",
        "total_due_with_carry_display",
    )
    fieldsets = (
         ("Personal Details", {"fields": ("company_name", "contact_name")}),
         ("Company Details", {"fields": ("country", "phone", "email", "address")}),
         ("Customer Account Stats", {"fields": ("previous_pending_balance_pkr", "lifetime_in_display", "total_material_display", "total_due_with_carry_display")}),
    )
    search_fields = ("company_name", "contact_name", "phone", "email")
    ordering = ("company_name",)
    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path(
                "<int:pk>/download-statement/",
                self.admin_site.admin_view(self.download_statement),
                name="core_customer_download_statement",
            ),
            path(
                "<int:pk>/preview-statement/",
                self.admin_site.admin_view(self.preview_statement),
                name="core_customer_preview_statement",
            ),
            path("<int:pk>/purchase-material/", self.admin_site.admin_view(self.purchase_material),
                 name="core_customer_purchase_material"),
            path("<int:pk>/sell-material/", self.admin_site.admin_view(self.sell_material),
                 name="core_customer_sell_material"),
            path("<int:pk>/transfer-material/", self.admin_site.admin_view(self.transfer_material),
                 name="core_customer_transfer_material"),
        ]
        return my_urls + urls
    # Pretty labels
    @admin.display(description="Total IN (Lifetime kg)")
    def lifetime_in_display(self, obj):
        return f"{obj.total_material_lifetime_kg:,.3f}"
    
    @admin.display(description="Total Material (kg)")
    def total_material_display(self, obj):
        return f"{obj.total_material_kg:,.3f}"

    @admin.display(description="Carry-Forward (PKR)")
    def carry_forward_display(self, obj):
        return f"PKR {obj.previous_pending_balance_pkr:,}"

    @admin.display(description="Pending + Carry (PKR)")
    def total_due_with_carry_display(self, obj):
        return f"PKR {obj.ar_total_due_with_carry_pkr:,}"
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
    
    
    
    def download_statement(self, request, pk):
        year, month = self._parse_period(request)
        if year is None:
            return HttpResponseBadRequest("Invalid year/month.")

        try:
            pdf_path = generate_customer_monthly_statement(pk, year, month, user=request.user)
        except Exception as e:
            return HttpResponseBadRequest(str(e))

        fname = Path(pdf_path).name
        # attachment = True forces download (keeps filename)
        return FileResponse(open(pdf_path, "rb"),
                           as_attachment=True,
                           filename=fname,
                           content_type="application/pdf")
    
    
    def preview_statement(self, request, pk):
        year, month = self._parse_period(request)
        if year is None:
            return HttpResponseBadRequest("Invalid year/month.")

        try:
            pdf_path = generate_customer_monthly_statement(pk, year, month, user=request.user)
        except Exception as e:
            return HttpResponseBadRequest(str(e))

        fname = Path(pdf_path).name
        resp = FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
        # inline + filename ⇒ shows in browser tab with correct name
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}

        # ----- Month/Year dropdown context -----
        today = now().date()
        months = [(i, month_name[i]) for i in range(1, 13)]
        years = list(range(today.year - 5, today.year + 2))

        default_month = int(request.GET.get("month", today.month))
        default_year  = int(request.GET.get("year",  today.year))

        download_url = reverse("admin:core_customer_download_statement", args=[object_id])
        preview_url  = reverse("admin:core_customer_preview_statement", args=[object_id])
        # Build filtered changelist links
        cash_ledger_url = (
            reverse("admin:core_payment_changelist")
            + f"?customer__id__exact={object_id}"
        )
        material_ledger_url = (
            reverse("admin:core_customermaterialledger_changelist")
            + f"?customer__id__exact={object_id}"
        )
        allocation_ledger_url = (
            reverse("admin:core_paymentallocation_changelist")
            + f"?order__customer__id__exact={object_id}"
        )
                
    
        extra_context.update({
        # statement widget
        "statement_download_url": download_url,
        "statement_preview_url": preview_url,
        "months": months,
        "years": years,
        "default_month": default_month,
        "default_year": default_year,
        # ledgers
        "cash_ledger_url": cash_ledger_url,
        "material_ledger_url": material_ledger_url,
        "allocation_ledger_url": allocation_ledger_url,
    })
        if request.user.is_superuser or request.user.has_perm("core.can_manage_material_trades"):
            extra_context["raw_material_tools"] = True
        return super().change_view(request, object_id, form_url, extra_context=extra_context)
    # ---- Views
    def _check_perm_or_403(self, request):
        if not (request.user.is_superuser or request.user.has_perm("core.can_manage_material_trades")):
            messages.error(request, "You do not have permission to manage raw material trades.")
            return redirect("admin:index")
     # --- Purchase Material   
    def _raw_material_base_ctx(self, request, customer, title):
        return {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "app_label": self.model._meta.app_label,   # ✅ keep breadcrumbs working
            "original": customer,                      # used by default admin header
            "title": title,
            "media": self.media,
            "has_view_permission": True,
        }
        
    def purchase_material(self, request, pk):
        self._check_perm_or_403(request)
        customer = Customer.objects.get(pk=pk)  # this 'customer' is just context
        ctx = self._raw_material_base_ctx(request, customer, f"Purchase Raw Material — {customer}")

        if request.method == "POST":
            supplier = (request.POST.get("supplier") or "").strip()
            qty_raw = (request.POST.get("qty_kg") or "").strip()
            rate_raw = (request.POST.get("unit_cost_pkr") or "").strip()
            notes = (request.POST.get("notes") or "").strip()

            errors = []
            if not supplier:
                errors.append("Supplier is required.")

            try:
                qty_kg = Decimal(qty_raw)
                if qty_kg <= 0:
                    raise ValueError()
            except Exception:
                errors.append("Quantity (kg) must be a positive number.")

            try:
                rate_pkr = int(rate_raw)
                if rate_pkr < 0:
                    raise ValueError()
            except Exception:
                errors.append("Unit cost (PKR/kg) must be a whole number ≥ 0.")

            if errors:
                for e in errors:
                    messages.error(request, e)
                return TemplateResponse(request, "core/admin/raw_material/purchase.html", ctx)

            # Create & apply
            try:
                with transaction.atomic():
                    txn = RawMaterialTxn(
                    kind=RawMaterialTxn.Kind.PURCHASE,
                    supplier_name=supplier,
                    qty_kg=qty_kg,
                    rate_pkr=rate_pkr,
                    memo=notes,
                )
                txn.apply(user=request.user)
                messages.success(request, f"Purchase recorded. Txn #{txn.pk}.")
            except Exception as e:
                messages.error(request, f"Could not record purchase: {e}")
                return TemplateResponse(request, "core/admin/raw_material/purchase.html", ctx)

            return redirect(reverse("admin:core_customer_change", args=[pk]))

        return TemplateResponse(request, "core/admin/raw_material/purchase.html", ctx)

    # ---------- SELL (Company Stock OUT → Customer IN) ----------
    def sell_material(self, request, pk):
        self._check_perm_or_403(request)
        # here, 'customer' is the buyer (to_customer)
        customer = Customer.objects.get(pk=pk)
        ctx = self._raw_material_base_ctx(request, customer, f"Sell Raw Material — {customer}")

        if request.method == "POST":
            qty_raw = (request.POST.get("qty_kg") or "").strip()
            rate_raw = (request.POST.get("unit_cost_pkr") or "").strip()
            notes = (request.POST.get("notes") or "").strip()

            errors = []
            try:
                qty_kg = Decimal(qty_raw)
                if qty_kg <= 0:
                    raise ValueError()
            except Exception:
                errors.append("Quantity (kg) must be a positive number.")

            try:
                rate_pkr = int(rate_raw)
                if rate_pkr < 0:
                    raise ValueError()
            except Exception:
                errors.append("Unit cost (PKR/kg) must be a whole number ≥ 0.")

            if errors:
                for e in errors:
                    messages.error(request, e)
                return TemplateResponse(request, "core/admin/raw_material/sell.html", ctx)

            try:
                with transaction.atomic():
                    txn = RawMaterialTxn(
                        kind=RawMaterialTxn.Kind.SALE,
                        to_customer=customer,     # the customer in URL
                        qty_kg=qty_kg,
                        rate_pkr=rate_pkr,        # the SELLING rate you type in the form
                        memo=notes,
                    )
                    txn.apply(user=request.user)
                messages.success(request, f"Sale recorded. Txn #{txn.pk}.")
            except Exception as e:
                messages.error(request, f"Could not record sale: {e}")
                return TemplateResponse(request, "core/admin/raw_material/sell.html", ctx)

            return redirect(reverse("admin:core_customer_change", args=[pk]))

        return TemplateResponse(request, "core/admin/raw_material/sell.html", ctx)
    def rm_sales_total_pkr(self, obj):
        agg = RawMaterialTxn.objects.filter(
            kind=RawMaterialTxn.Kind.SALE, to_customer=obj
        ).aggregate(s=Sum("amount_pkr"))
        return f"PKR { (agg['s'] or 0):, }"
    rm_sales_total_pkr.short_description = "Raw Material Sales (PKR)"
    # ---------- TRANSFER (Customer → Customer) ----------
    def transfer_material(self, request, pk):
        self._check_perm_or_403(request)
        # Here, 'customer' is the FROM party (source)
        from_customer = Customer.objects.get(pk=pk)
        ctx = self._raw_material_base_ctx(request, from_customer, f"Transfer Raw Material — {from_customer}")

        if request.method == "POST":
            to_id_raw = (request.POST.get("to_customer_id") or "").strip()
            qty_raw = (request.POST.get("qty_kg") or "").strip()
            notes = (request.POST.get("notes") or "").strip()

            errors = []
            try:
                to_id = int(to_id_raw)
                to_customer = Customer.objects.get(pk=to_id)
            except Exception:
                to_customer = None
                errors.append("Select a valid target customer.")

            try:
                qty_kg = Decimal(qty_raw)
                if qty_kg <= 0:
                    raise ValueError()
            except Exception:
                errors.append("Quantity (kg) must be a positive number.")

            if to_customer and to_customer.pk == from_customer.pk:
                errors.append("From/To customers must be different.")

            # Optional rate for transfers (set to 0 if not used)
            rate_pkr = int((request.POST.get("unit_cost_pkr") or 0) or 0)

            if errors:
                for e in errors:
                    messages.error(request, e)
                # Provide list of other customers in ctx if your template needs it
                ctx["other_customers"] = Customer.objects.exclude(pk=from_customer.pk)
                return TemplateResponse(request, "core/admin/raw_material/transfer.html", ctx)

            try:
                with transaction.atomic():
                    txn = RawMaterialTxn(
                        kind=RawMaterialTxn.Kind.TRANSFER,
                        from_customer=from_customer,
                        to_customer=to_customer,
                        qty_kg=qty_kg,
                        rate_pkr=rate_pkr,
                        memo=notes,
                    )
                    txn.apply(user=request.user)
                messages.success(request, f"Transfer recorded. Txn #{txn.pk}.")
            except Exception as e:
                messages.error(request, f"Could not record transfer: {e}")
                ctx["other_customers"] = Customer.objects.exclude(pk=from_customer.pk)
                return TemplateResponse(request, "core/admin/raw_material/transfer.html", ctx)

            return redirect(reverse("admin:core_customer_change", args=[pk]))

        # GET: you probably want a dropdown of other customers
        ctx["other_customers"] = Customer.objects.exclude(pk=from_customer.pk)
        return TemplateResponse(request, "core/admin/raw_material/transfer.html", ctx)

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        # Sum of IN deltas (positive)
        qs = qs.annotate(
            material_in=Coalesce(
                Sum(
                    "material_ledger__delta_kg",
                    filter=Q(material_ledger__delta_kg__gt=0),
                    output_field=KG,
                ),
                Value(Decimal("0"), output_field=KG),
            ),
            # Sum of negative deltas (OUT) as a negative number
            material_out_neg=Coalesce(
                Sum(
                    "material_ledger__delta_kg",
                    filter=Q(material_ledger__delta_kg__lt=0),
                    output_field=KG,
                ),
                Value(Decimal("0"), output_field=KG),
            ),
        ).annotate(
            # Turn negative OUT into a positive used kg
            out_kg=ExpressionWrapper(-F("material_out_neg"), output_field=KG),
            # Available = IN - OUT
            available_kg=ExpressionWrapper(F("material_in") - F("out_kg"), output_field=KG),
            # Total production == OUT (what you actually produced/consumed)
            production_kg=F("out_kg"),
        )

        return qs

    # Displays
    def total_production_display(self, obj):
        return f"{(obj.production_kg or Decimal('0')):,.3f} kg"
    total_production_display.short_description = "Total Production"
    total_production_display.admin_order_field = "production_kg"

    def total_available_material_display(self, obj):
        return f"{(obj.available_kg or Decimal('0')):,.3f} kg"
    total_available_material_display.short_description = "Available Material"
    total_available_material_display.admin_order_field = "available_kg"

def _company_stock():
    return RawMaterialTxn.company_stock_customer()

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

class RawMaterialTxnAdmin(admin.ModelAdmin):
    form = RawMaterialTxnAdminForm
    list_display = ("id","kind","when","supplier_name","to_customer","qty_kg","rate_pkr","amount_pkr")
    list_filter  = ("kind","when","supplier_name")
    search_fields = ("supplier_name","to_customer__company_name","memo")
    date_hierarchy = "when"
    # Don’t ever show these in the form

    exclude = ("amount_pkr", "from_customer", "created_by",)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)

        # figure out kind being edited/created
        kind = request.POST.get("kind") or getattr(obj, "kind", RawMaterialTxn.Kind.PURCHASE)

        # Hide company-stock side automatically
        # PURCHASE -> to_customer is always company stock (hide it)
        # SALE     -> from_customer is always company stock (already excluded)
        if "to_customer" in form.base_fields and kind == RawMaterialTxn.Kind.PURCHASE:
            form.base_fields["to_customer"].widget = forms.HiddenInput()

        # amount_pkr is excluded; if you want a read-only display, add a readonly field instead

        # Optional: relabel fields for clarity
        if "when" in form.base_fields:
            form.base_fields["when"].label = "Date of Purchase"
        if "rate_pkr" in form.base_fields:
            form.base_fields["rate_pkr"].label = "Rate of Purchase (PKR/kg)"
        if "supplier_name" in form.base_fields:
            form.base_fields["supplier_name"].label = "Supplier Name"

        return form
    def _paid_total(self, obj):
        if not obj:
            return 0
        # adjust path if your Payment.amount field differs
        return sum((link.payment.amount or 0) for link in obj.linked_payments.select_related("payment"))

    def paid_total_display(self, obj):
        return f"PKR {self._paid_total(obj):,}" if obj else "—"
    paid_total_display.short_description = "Paid (linked payments)"

    def remaining_display(self, obj):
        if not obj:
            return "—"
        remaining = (obj.amount_pkr or 0) - self._paid_total(obj)
        return f"PKR {remaining:,}"
    remaining_display.short_description = "Remaining"

    def amount_display(self, obj):
        return f"PKR {obj.amount_pkr:,}" if obj and obj.amount_pkr else "—"
    amount_display.short_description = "Amount (PKR)"

    def get_fields(self, request, obj=None):
        # Order the form fields as you requested and show a read-only total next to rate
        return [
            "kind",
            "material_type",
            "when",              # Date of Purchase
            "supplier_name",
            "dc_number",
            "bags_count",        # Bags (25kg each)
            "rate_pkr",
            "to_customer",       # visible for SALE, hidden by form for PURCHASE
            "memo",
        ]

@transaction.atomic
def save_model(self, request, obj, form, change):
    if not obj.created_by_id:
        obj.created_by = request.user

    cs = RawMaterialTxn.company_stock_customer()
    if obj.kind == RawMaterialTxn.Kind.PURCHASE:
        obj.from_customer = None
        obj.to_customer = cs
    elif obj.kind == RawMaterialTxn.Kind.SALE:
        obj.from_customer = cs

    if change:
        # EDIT: just save fields; do NOT re-apply ledger to avoid duplicates
        obj.full_clean()
        obj.save()
    else:
        # CREATE: write ledger entries
        obj.apply(user=request.user)

admin.site.register(RawMaterialTxn, RawMaterialTxnAdmin)

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
    list_display  = ('customer', 'date', 'type', 'delta_kg', 'order', 'receipt', 'memo')
    list_filter   = ('type', 'customer', 'date')
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
    list_display = ("invoice_number", "customer", "status", "grand_total_display", "total_allocated_display", "outstanding_balance_display",  "target_total_kg" ,"produced_kg")
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
    
class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Branding", {"fields": ("company_name", "company_address", "logo")}),
        ("Tax", {"fields": ("tax_label", "tax_rate")}),
        ("Banking", {"fields": ("notes","bank_details")}),
        ("Email (optional)", {"fields": ("email_host", "email_port", "email_use_tls", "email_host_user", "email_host_password"), "classes": ("collapse",)}),
    )
    list_display = ("company_name", "tax_label", "tax_rate")
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # DEBUG: see fields the form thinks it has
        print("SiteSettingsAdmin fields ->", list(form.base_fields.keys()))
        return form


