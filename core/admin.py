from django.contrib import admin, messages
from django.urls import path, reverse
from django import forms
from django.core.exceptions import ValidationError
from django.http import FileResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.utils.html import format_html
from pathlib import Path
from .material_sync import sync_order_material_ledger  # <-- add this at the top
from .models import Order, OrderItem, OrderRoll, Customer, Payment, ExpenseCategory, Expense, Employee, Attendance, SalaryPayment, MaterialReceipt, CustomerMaterialLedger  # if you have Payment
from .utils import generate_invoice, send_invoice_email, generate_customer_monthly_statement
from django.shortcuts import redirect, get_object_or_404
from decimal import Decimal
from django.db import models
from django.db import transaction
from django.db.models import Sum, F, Value, ExpressionWrapper, DecimalField, Q, Subquery,OuterRef, Case, When
from django.db.models.functions import Coalesce
from django.utils.html import format_html
from django.utils.timezone import now
from calendar import month_name


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
# def _items_changed(formsets):
#     """
#     Returns True if the OrderItem inline had any add/edit/delete in this request.
#     Works in save_related (post-save) by checking initial/instance deltas.
#     """
#     from .models import OrderItem
#     for fs in formsets:
#         if getattr(fs, 'model', None) is OrderItem:
#             # If any form has_changed, or a new instance was created, or marked DELETE
#             for f in fs.forms:
#                 if getattr(f, 'cleaned_data', None) and f.cleaned_data.get('DELETE'):
#                     return True
#                 if f.instance.pk is None and getattr(f, 'has_changed', lambda: False)():
#                     return True
#                 if getattr(f, 'has_changed', lambda: False)():
#                     return True
#     return False

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



def _orders_with_totals(qs):
    """
    Avoid cartesian multiplication by using separate subqueries for item total and payments total.
    """
    money = DecimalField(max_digits=18, decimal_places=2)

    # Per-item line total
    item_amount_expr = ExpressionWrapper(
        F('roll_weight') * F('quantity') * F('price_per_kg'),
        output_field=money,
    )

    # Subquery: total of items per order
    items_total_sq = (
        OrderItem.objects
        .filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum(item_amount_expr), Value(0, output_field=money)))
        .values('total')[:1]
    )

    # Subquery: total of payments per order
    payments_total_sq = (
        Payment.objects
        .filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum('amount'), Value(0, output_field=money)))
        .values('total')[:1]
    )

    return qs.annotate(
        total_amount_calc=Coalesce(Subquery(items_total_sq, output_field=money), Value(0, output_field=money)),
        total_paid_calc=Coalesce(Subquery(payments_total_sq, output_field=money), Value(0, output_field=money)),
    ).annotate(
        outstanding_amount_calc=ExpressionWrapper(
            F('total_amount_calc') - F('total_paid_calc'),
            output_field=money
        )
    )
@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    change_form_template = "core/admin/core/customer/change_form.html"
    list_display = (
        "company_name",
        "contact_name",
        "email",
        "total_production_display",        # == material used (OUT)
        "total_available_material_display",# IN - OUT
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
        ]
        return my_urls + urls
    
    def _parse_period(self, request):
        """Get (year, month) from GET with safe defaults and validation."""
        today = now().date()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
        except ValueError:
            return None, None
        if not (2000 <= year <= 2100) or not (1 <= month <= 12):
            return None, None
        return year, month
    
    
    
    def download_statement(self, request, pk):
        # Default period = current month, unless provided
        year, month = self._parse_period(request)
        if year is None:
            return HttpResponseBadRequest("Invalid year/month.")

        try:
            pdf_path = generate_customer_monthly_statement(pk, year, month)
        except Exception as e:
            return HttpResponseBadRequest(str(e))

        fname = Path(pdf_path).name
        return FileResponse(open(pdf_path, "rb"), as_attachment=True, filename=fname)
    
    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}

        # Build context for the month/year dropdowns
        today = now().date()
        months = [(i, month_name[i]) for i in range(1, 13)]
        # years list: last 5 years to next 1 year (tweak as you like)
        years = list(range(today.year - 5, today.year + 2))

        download_url = reverse("admin:core_customer_download_statement", args=[object_id])
        extra_context.update({
            "statement_download_url": download_url,
            "months": months,
            "years": years,
            "default_month": int(request.GET.get("month", today.month)),
            "default_year": int(request.GET.get("year", today.year)),
        })

        return super().change_view(request, object_id, form_url, extra_context=extra_context)
    
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
    
class PaymentInlineFormset(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()

        # If we're creating a NEW order (no PK yet), we cannot safely
        # compute existing payments/outstanding. Just validate amounts > 0,
        # and skip the overpayment total check for now.
        order = getattr(self, "instance", None)
        if not order or not order.pk:
            for form in self.forms:
                if not hasattr(form, "cleaned_data"):
                    continue
                if form.cleaned_data.get("DELETE"):
                    continue
                amt = form.cleaned_data.get("amount")
                if amt is None or Decimal(amt) <= 0:
                    raise ValidationError("Payment amount must be greater than 0.")
            return  # <-- important: skip cumulative overpay check on add

        # ---- Existing logic for change view (order has PK) ----
        from decimal import Decimal
        from django.db.models import DecimalField, Sum

        # existing payments excluding ones being edited/deleted now
        editing_ids = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            if form.instance and form.instance.pk:
                editing_ids.add(form.instance.pk)

        existing_qs = order.payments.all()
        if editing_ids:
            existing_qs = existing_qs.exclude(pk__in=editing_ids)

        existing_paid = sum(Decimal(p.amount or 0) for p in existing_qs)

        batch_total = Decimal("0")
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            amt = form.cleaned_data.get("amount") or Decimal("0")
            if Decimal(amt) <= 0:
                raise ValidationError("Payment amount must be greater than 0.")
            batch_total += Decimal(amt)

        order_total = Decimal(order.total_amount or 0)
        if existing_paid + batch_total > order_total:
            raise ValidationError(
                f"Overpayment: total would be {(existing_paid + batch_total):,.2f} "
                f"on an order of {order_total:,.2f}. Reduce the payment amount(s)."
            )

class PaymentInline(admin.TabularInline):
    model = Payment
    formset = PaymentInlineFormset
    extra = 0
    fields = ('payment_date', 'amount', 'payment_method', 'notes')  # method + notes now exist
    readonly_fields = ('payment_date',)
    ordering = ('-payment_date',)

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

@admin.register(CustomerMaterialLedger)
class CustomerMaterialLedgerAdmin(admin.ModelAdmin):
    list_display = ('customer', 'date', 'type', 'delta_kg', 'order', 'receipt', 'memo')
    list_filter = ('type', 'customer', 'date')
    search_fields = ('customer__company_name', 'memo')


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
        # ensure Decimal math
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

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    change_form_template = 'core/order_change_form.html'
    formset = OrderItemInlineFormSet,
    list_display = (
        "invoice_number",
        "customer",
        "colored_status",
        "target_total_kg",
        "price_per_kg",
        "produced_kg",
        "remaining_kg",
        "subtotal_display",
        "tax_display",
        "grand_total_display",
        "total_paid_display",
        "outstanding_display",
    )
    
    list_filter = ("status", "customer")
    search_fields = ("customer__company_name", "invoice_number")
    inlines = [OrderRollInline, PaymentInline]
    actions = ["include_gst_on", "include_gst_off"]
    fieldsets = (
        ("Customer & Terms", {"fields": ("customer", "payment_terms", "delivery_date",)}),
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
        return f"{obj.grand_total:,.2f}"
    grand_total_display.short_description = "Grand Total"

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

    def outstanding_display(self, obj):
        return f"{obj.outstanding_balance:,.2f}"
    outstanding_display.short_description = "Outstanding"

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
        if new_status not in dict(Order.STATUS):
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


# core/admin.py
from decimal import Decimal
from django.contrib import admin
from .models import SiteSettings, Customer  # keep your other imports

class SiteSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Branding", {"fields": ("company_name", "company_address", "logo")}),
        ("Tax", {"fields": ("tax_label", "tax_rate")}),
        ("Banking", {"fields": ("bank_details",)}),
        ("Email (optional)", {"fields": ("email_host", "email_port", "email_use_tls", "email_host_user", "email_host_password"), "classes": ("collapse",)}),
    )
    list_display = ("company_name", "tax_label", "tax_rate")
# admin.site.register(Payment)  # if not already

