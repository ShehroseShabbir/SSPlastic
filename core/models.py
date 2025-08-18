from django.db import models
from django.core.exceptions import ValidationError, NON_FIELD_ERRORS
from django.core.validators import MinValueValidator
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from django.db.models import Sum, Q, UniqueConstraint
from django.conf import settings
from .models_ar import FINAL_STATES

TWOPLACES = Decimal('0.01')

MICRON_HELP = "Use syntax like 45/90 (not just a number)."

CONSUME_STATUSES = ("CONFIRMED", "INPROD", "READY", "DELIVERED", "SHIPPED", "CLOSED")

TWOKG = Decimal('0.001')
def dkg(x):  # keep your helper consistent
    return (Decimal(str(x or 0))).quantize(TWOKG)
def parse_weights_csv(text):
    """
    Accepts comma/space separated values like: '12.5, 13, 10.75'
    Returns list[Decimal] (>=0).
    """
    if not text:
        return []
    raw = [p.strip() for p in text.replace("\n", ",").split(",")]
    vals = []
    for s in raw:
        if not s:
            continue
        vals.append(Decimal(s))
    return vals

from decimal import Decimal, ROUND_HALF_UP
TWOKG = Decimal('0.001')  # keep 3 decimal places
def dkg(x):  # to Decimal kg
    return (Decimal(str(x or 0))).quantize(TWOKG, rounding=ROUND_HALF_UP)

# MaterialReceipt
class MaterialReceipt(models.Model):
    """
    When a customer sends raw polythene bags as material.
    1 bag = 25 kg typically, but allow free entry too.
    """
    BAG_WEIGHT_KG = Decimal('25')  # default bag size

    customer = models.ForeignKey('Customer', on_delete=models.CASCADE, related_name='material_receipts')
    date = models.DateField(auto_now_add=True)
    bags_count = models.PositiveIntegerField(default=0)         # e.g. 4 bags
    extra_kg = models.DecimalField(max_digits=10, decimal_places=3, default=0)  # non-bag kg if any
    notes = models.CharField(max_length=255, blank=True)

    @property
    def total_kg(self):
        return dkg(self.bags_count) * self.BAG_WEIGHT_KG + dkg(self.extra_kg)

    def __str__(self):
        return f"{self.customer.company_name} · {self.date} · {self.total_kg} kg"
    
class CustomerMaterialLedger(models.Model):
    class EntryType(models.TextChoices):
        IN = "IN", "In"
        OUT = "OUT", "Out"

    customer = models.ForeignKey(
        "Customer", on_delete=models.CASCADE, related_name="material_ledger"
    )
    # keep ONLY ONE order FK
    order = models.ForeignKey(
        "Order", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="material_ledger_entries"
    )
    receipt = models.ForeignKey(
        "MaterialReceipt", null=True, blank=True, on_delete=models.SET_NULL
    )

    date = models.DateTimeField(auto_now_add=True)
    type = models.CharField(max_length=3, choices=EntryType.choices)
    delta_kg = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0.000"))
    memo = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["order", "type"],
                name="uniq_out_per_order",
                condition=Q(type="OUT"),
            )
        ]

    def __str__(self):
        return f"{self.customer.company_name} {self.type} {self.delta_kg} kg"


# Customer
class Customer(models.Model):

    company_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=100, default="Pakistan")  # or select list
    phone = models.CharField(max_length=20, blank=True)  # validate by country
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)  

    @property
    def ar_invoices_total(self) -> Decimal:
        """
        Sum of billable (final-state) invoices using the Order.grand_total PROPERTY.
        Must be computed in Python, not via DB aggregation.
        """
        total = Decimal("0.00")
        qs = self.orders.filter(status__in=FINAL_STATES).select_related("customer").prefetch_related("rolls")
        for o in qs:
            total += (o.grand_total or Decimal("0.00"))
        return total.quantize(Decimal("0.01"))

    @property
    def ar_allocations_total(self) -> Decimal:
        # this one is fine (amount is a real field)
        from .models_ar import PaymentAllocation
        agg = PaymentAllocation.objects.filter(
            order__customer=self,
            order__status__in=FINAL_STATES
        ).aggregate(s=Sum("amount"))
        return (Decimal(agg["s"] or 0)).quantize(Decimal("0.01"))

    @property
    def ar_unapplied_payments(self) -> Decimal:
        total = Decimal("0.00")
        for p in self.payments_ar.all():
            total += (p.unapplied_amount or Decimal("0.00"))
        return total.quantize(Decimal("0.01"))

    @property
    def ar_pending_balance(self) -> Decimal:
        return (self.ar_invoices_total - self.ar_allocations_total).quantize(Decimal("0.01"))

    @property
    def ar_net_position(self) -> Decimal:
        return (self.ar_pending_balance - self.ar_unapplied_payments).quantize(Decimal("0.01"))

    @property
    def material_balance_kg(self):
        """Total raw material available for this customer in kg."""
        agg = self.material_ledger.aggregate(s=Sum('delta_kg'))
        return agg['s'] or Decimal('0')
    def __str__(self):
        return self.company_name
    


# Order


class Order(models.Model):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("CONFIRMED", "Confirmed"),
        ("INPROD", "In Production"),
        ("READY", "Ready for Shipping"),
        ("DELIVERED", "Delivered"),
        ("CLOSED", "Closed"),
    ]

    PAYMENT_TERMS = [
        ("NET7", "Net 7"),
        ("NET14", "Net 14"),
        ("NET30", "Net 30"),
        ("NET45", "Net 45"),
        ("CASH", "Cash"),
        ("Bank Transfer", "Bank Transfer"),
    ]

    customer = models.ForeignKey("Customer", on_delete=models.CASCADE, related_name="orders")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="DRAFT")
    order_date = models.DateField(auto_now_add=True)
    delivery_date = models.DateField(blank=True, null=True)
    payment_terms = models.CharField(max_length=20, choices=PAYMENT_TERMS, blank=True)

    include_gst = models.BooleanField(default=True, help_text="Include GST for this order")
    delivery_challan = models.CharField(max_length=20, default="", null=True, blank=True)
    delivery_challan_date = models.DateField(blank=True, null=True)

    # Booking “category”
    roll_size = models.CharField(max_length=50, default=0)      # e.g. "21"
    micron = models.CharField(
    max_length=20,
    null=True,
    help_text=MICRON_HELP
)       # e.g. "45/90"
    current_type = models.CharField(max_length=10, choices=[("NT","NT"),("DT","DT"),("ST","ST")], null=True)

    # Targets & pricing
    target_total_kg = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0.000"))

    # Kept for now (signals make this flag unnecessary; consider removing later)
    material_deducted = models.BooleanField(default=False)

    price_per_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    invoice_number = models.CharField(max_length=50, unique=True, blank=True, null=True)

    tolerance_kg = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("0.500"),
        help_text="Allowed diff between produced and target at READY/DELIVERED/CLOSED"
    )

    # ----------------- Derived amounts -----------------

    @property
    def produced_kg(self) -> Decimal:
        """
        Sum of produced roll weights for this order (uses related_name='rolls').
        Safe during Add (no PK) → returns 0.
        """
        if self._state.adding or not self.pk:
            return Decimal("0.000")
        agg = self.rolls.aggregate(s=Sum("weight_kg"))
        return dkg(agg["s"])

    @property
    def remaining_kg(self) -> Decimal:
        return dkg((self.target_total_kg or 0) - (self.produced_kg or 0))

    @property
    def billable_kg(self) -> Decimal:
        """
        Before READY: bill target (booking). After READY: bill produced.
        """
        if self.status in ("READY", "DELIVERED", "CLOSED"):
            return dkg(self.produced_kg)
        return dkg(self.target_total_kg)

    @property
    def subtotal(self) -> Decimal:
        amt = (self.billable_kg or 0) * (Decimal(str(self.price_per_kg)) if self.price_per_kg is not None else Decimal("0"))
        return (amt).quantize(Decimal("0.01"))

    @property
    def tax_rate_ratio(self) -> Decimal:
        if not self.include_gst:
            return Decimal("0")
        try:
            from .utils_settings import get_site_settings
            ss = get_site_settings()
            if ss and ss.tax_rate is not None:
                return (Decimal(ss.tax_rate) / Decimal("100")).quantize(Decimal("0.0001"))
        except Exception:
            pass
        return Decimal(str(getattr(settings, "TAX_RATE", 0) or 0))

    @property
    def tax_amount(self) -> Decimal:
        return (self.subtotal * self.tax_rate_ratio).quantize(Decimal("0.01"))

    @property
    def grand_total(self) -> Decimal:
        return (self.subtotal + self.tax_amount).quantize(Decimal("0.01"))

    @property
    def total_amount(self) -> Decimal:
        # alias for pre-tax subtotal (kept for backward compatibility)
        return self.subtotal

    @property
    def total_allocated(self) -> Decimal:
        agg = self.payment_allocations.aggregate(s=Sum("amount"))
        return (Decimal(agg["s"] or 0)).quantize(Decimal("0.01"))

    @property
    def total_paid(self) -> Decimal:
        # Backward-compatible alias: now it's “allocated to this order”
        return self.total_allocated

    @property
    def outstanding_balance(self) -> Decimal:
        return (self.grand_total - self.total_allocated).quantize(Decimal("0.01"))

    # ----------------- Validation (read-only) -----------------

    def clean(self):
        """
        Transition-aware checks only:
        - On (→ CONFIRMED) ensure enough material (no write).
        - On first move into READY/DELIVERED/CLOSED, enforce tolerance.
        Never touch reverse rels while adding.
        """
        super().clean()

        # Don’t inspect children/transitions before we have a row
        if self._state.adding or not self.pk:
            return

        # Old status → decide transition
        old_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()

        # DRAFT/whatever → CONFIRMED: ensure enough material to reserve
        moving_to_confirmed = (old_status != "CONFIRMED") and (self.status == "CONFIRMED")
        if moving_to_confirmed:
            need = dkg(self.target_total_kg)
            available = dkg(self.customer.material_balance_kg)
            if need > available:
                raise ValidationError({
                    "__all__": (
                        f"Insufficient raw material for this order. "
                        f"Required: {need:,.3f} kg, Available: {available:,.3f} kg."
                    )
                })

        # First entry into a final state → enforce tolerance
        was_final = old_status in ("READY", "DELIVERED", "CLOSED")
        now_final = self.status in ("READY", "DELIVERED", "CLOSED")
        moving_to_final = (not was_final) and now_final
        if moving_to_final:
            produced = dkg(self.produced_kg)  # safe: instance is saved
            target = dkg(self.target_total_kg)
            diff = abs(produced - target)
            tol = dkg(self.tolerance_kg or Decimal("0"))
            if diff > tol:
                raise ValidationError(
                    f"Produced {produced} kg deviates from target {target} kg by {diff} kg. "
                    f"Tolerance: {self.tolerance_kg} kg."
                )

    # ----------------- Save (no deduction here; signals handle it) -----------------

    def save(self, *args, **kwargs):
        # Keep invoice number generation, but DO NOT deduct material here.
        if not self.invoice_number:
            last = type(self).objects.order_by("-id").only("id").first()
            next_id = (last.id + 1) if last else 1
            self.invoice_number = f"SSP-{next_id:05d}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.invoice_number} - {getattr(self.customer, 'company_name', self.customer_id)}"

class OrderRoll(models.Model):
    """
    A single produced roll weight. Add many of these during production (scan or manual).
    """
    order = models.ForeignKey(Order, related_name="rolls", on_delete=models.CASCADE)
    weight_kg = models.DecimalField(max_digits=7, decimal_places=3,
                                    validators=[MinValueValidator(Decimal("0.001"))])
    barcode = models.CharField(max_length=64, blank=True, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.order_id} · {self.weight_kg} kg"

# Order Item
class OrderItem(models.Model):
    CURRENT_TYPES = [('NT','NT'), ('DT','DT'), ('ST','ST')]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    roll_size = models.FloatField()
    micron = models.CharField(max_length=10)  # change from IntegerField to CharField
    current_type = models.CharField(max_length=2, choices=CURRENT_TYPES)
    roll_weight = models.FloatField()
    quantity = models.IntegerField()
    price_per_kg = models.FloatField()

    @property
    def total_price(self) -> Decimal:
        return (Decimal(str(self.price_per_kg or 0)) *
                Decimal(str(self.roll_weight or 0)) *
                Decimal(str(self.quantity or 0))).quantize(Decimal("0.01"))
    
    @property
    def total_weight(self):
        # weight contributed by this line
        w = Decimal (str(self.roll_weight or 0))
        q = Decimal (str(self.quantity or 0))
        return (w * q).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    


    def __str__(self):
        return f"Roll {self.roll_size} - {self.current_type}"


# -------------------------
# EXPENSES
# -------------------------
class ExpenseCategory(models.Model):
    """
    Categories like Electricity (KE), Gas, Factory Expense, Labour Cost etc.
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Expense Categories"

    def __str__(self):
        return self.name


class Expense(models.Model):
    """
    Individual expense entries, optionally monthly.
    """
    PERIOD_CHOICES = [
        ('ONCE', 'One-time'),
        ('MONTHLY', 'Monthly'),
        ('YEARLY', 'Yearly'),
    ]
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='expenses')
    title = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    expense_date = models.DateField(default=timezone.now)
    period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='ONCE')
    notes = models.TextField(blank=True)
    attachment = models.FileField(upload_to='expenses/', blank=True, null=True)  # e.g., bill scan/PDF

    def __str__(self):
        return f"{self.title} - {self.amount} ({self.category.name})"
# -------------------------
# EMPLOYEES / LABOUR
# -------------------------
class Employee(models.Model):
    """
    Store labour/staff info + photo and contract/documents.
    """
    name = models.CharField(max_length=150)
    cnic = models.CharField(max_length=20, blank=True)  # Pakistan CNIC format e.g., 42101-1234567-1
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # monthly salary
    join_date = models.DateField(null=True, blank=True)
    hire_date = models.DateField(null=True, blank=True)  # alias if needed separately
    contract_end_date = models.DateField(null=True, blank=True)

    profile_picture = models.ImageField(upload_to='employees/photos/', null=True, blank=True)
    contract_file = models.FileField(upload_to='employees/contracts/', null=True, blank=True)

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Attendance(models.Model):
    """
    Daily attendance for employees.
    """
    STATUS_CHOICES = [
        ('P', 'Present'),
        ('A', 'Absent'),
        ('L', 'Leave'),
        ('H', 'Half Day'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default='P')
    hours_worked = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # optional
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ('employee', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee.name} - {self.date} - {self.get_status_display()}"


class SalaryPayment(models.Model):
    """
    Records monthly salary payments to employees.
    """
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='salary_payments')
    period_month = models.PositiveSmallIntegerField()  # 1-12
    period_year = models.PositiveIntegerField()
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_date = models.DateField(default=timezone.now)
    method = models.CharField(max_length=40, default='Cash', blank=True)  # Cash/Bank Transfer/Cheque
    notes = models.TextField(blank=True)
    slip = models.FileField(upload_to='employees/salary_slips/', null=True, blank=True)

    class Meta:
        unique_together = ('employee', 'period_month', 'period_year')
        ordering = ['-period_year', '-period_month']

    @property
    def outstanding(self):
        return (self.gross_amount or 0) - (self.paid_amount or 0)

    def __str__(self):
        return f"{self.employee.name} - {self.period_month}/{self.period_year}"

class SiteSettings(models.Model):
    # Branding / company info
    company_name = models.CharField(max_length=255, blank=True, default="")
    company_address = models.TextField(blank=True, default="")  # multi-line
    bank_details = models.TextField(blank=True, default="")     # multi-line
    logo = models.ImageField(upload_to="branding/", null=True, blank=True)

    # Tax
    tax_label = models.CharField(max_length=20, default="GST", blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))  # e.g., 17.00

    # Optional SMTP overrides (leave blank to use project settings.py)
    email_backend = models.CharField(max_length=200, blank=True, default="", help_text="e.g. django.core.mail.backends.smtp.EmailBackend")
    email_host = models.CharField(max_length=200, blank=True, default="")
    email_port = models.PositiveIntegerField(null=True, blank=True)
    email_use_tls = models.BooleanField(default=True)
    email_host_user = models.CharField(max_length=200, blank=True, default="")
    email_host_password = models.CharField(max_length=200, blank=True, default="", help_text="Consider env vars instead of storing here.")

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return self.company_name or "Site Settings"

    # Helpers to turn TextField → list of lines
     # Helpers used by the invoice code
    @property
    def company_address_list(self):
        return [ln.strip() for ln in (self.company_address or "").splitlines() if ln.strip()]

    @property
    def bank_details_list(self):
        return [ln.strip() for ln in (self.bank_details or "").splitlines() if ln.strip()]

