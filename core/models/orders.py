from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.core.exceptions import ValidationError, NON_FIELD_ERRORS
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import path
from core.utils_weight import D, dkg
from ..utils_settings import get_site_settings
from .common import MICRON_CHOICES, CURRENT_TYPES, MICRON_HELP, COUNTRIES
from django.db.models import Sum
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseRedirect



# ---------- Helpers (use your central utils; safe fallbacks included) ----------
try:
    # canonical money/weight helpers you said you use everywhere
    from core.utils_money import to_rupees_int as _to_pkr_int, pkr_str as _pkr_str
except Exception:
    def _to_pkr_int(x) -> int:
        try:
            return int(Decimal(str(x or 0)).quantize(Decimal("1")))
        except Exception:
            return 0
    def _pkr_str(n: int) -> str:
        return f"PKR {int(n):,}"

try:
    from core.utils_weight import dkg  # Decimal → quantize to 3dp
except Exception:
    def dkg(val) -> Decimal:
        if val is None:
            val = Decimal("0")
        return Decimal(str(val)).quantize(Decimal("0.001"))

try:
    from core.utils_settings import get_site_settings
except Exception:
    def get_site_settings():
        return None

D = Decimal  # convenience

class Order(models.Model):
    save_on_top = True
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
        ("NET60", "Net 60"),
        ("NET90", "Net 90"),
    ]

    # Expect these constants come from your formatting module

    customer = models.ForeignKey("Customer", on_delete=models.CASCADE, related_name="orders")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="DRAFT")
    order_date = models.DateField(auto_now_add=True)
    delivery_date = models.DateField(blank=True, null=True)
    payment_terms = models.CharField(max_length=20, choices=PAYMENT_TERMS, blank=True)

    include_gst = models.BooleanField(default=False, help_text="Include GST for this order")
    delivery_challan = models.CharField(max_length=20, default="", null=True, blank=True)
    delivery_challan_date = models.DateField(blank=True, null=True)

    # Booking “category”
    roll_size = models.CharField(max_length=50, default=0)  # e.g. "21"
    micron = models.CharField(max_length=10, null=True, help_text=MICRON_HELP, choices=MICRON_CHOICES)
    current_type = models.CharField(max_length=10, choices=CURRENT_TYPES, null=True)

    # Targets & pricing
    target_total_kg = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0.000"))

    # Kept (signals handle deduction)
    material_deducted = models.BooleanField(default=False)

    # NOTE: price_per_kg stays Decimal field (unit price), but all totals are PKR-int.
    price_per_kg = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    invoice_number = models.CharField(
        max_length=50, unique=True, blank=True, null=True,
        help_text="Automatically generated do not edit"
    )

    tolerance_kg = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("0.500"),
        help_text="Allowed diff between produced and target at READY/DELIVERED/CLOSED"
    )

    # ----------------- Derived amounts (kg) -----------------
    @property
    def produced_kg(self) -> Decimal:
        """Sum of produced roll weights for this order (uses related_name='rolls')."""
        if self._state.adding or not self.pk:
            return Decimal("0.000")
        agg = self.rolls.aggregate(s=Sum("weight_kg"))
        return dkg(agg["s"])

    @property
    def remaining_kg(self) -> Decimal:
        return dkg((self.target_total_kg or 0) - (self.produced_kg or 0))

    @property
    def billable_kg(self) -> Decimal:
        """Before READY: bill target; after READY: bill produced."""
        if self.status in ("READY", "DELIVERED", "CLOSED"):
            return dkg(self.produced_kg)
        return dkg(self.target_total_kg)

    # ----------------- ONE SOURCE OF TRUTH for money (PKR int) -----------------
    # All money-related properties below are canonical PKR integers (no decimals).

    @property
    def subtotal_pkr(self) -> int:
        """
        Canonical: subtotal as PKR int.
        Compute using Decimal math (kg * price), then convert to rupees int.
        """
        kg = D(str(self.billable_kg or 0))
        rate = D(str(self.price_per_kg or 0))
        subtotal_dec = kg * rate  # do not quantize to .01; directly map to rupees int
        return _to_pkr_int(subtotal_dec)

    @property
    def tax_rate_ratio(self) -> Decimal:
        """Tax ratio as Decimal (e.g., 0.17)."""
        if not self.include_gst:
            return D("0")
        try:
            ss = get_site_settings()
            if ss and ss.tax_rate is not None:
                return (D(ss.tax_rate) / D("100"))
        except Exception:
            pass
        return D(str(getattr(settings, "TAX_RATE", 0) or 0))

    @property
    def tax_amount_pkr(self) -> int:
        """Canonical: tax as PKR int (derived from subtotal * tax_rate_ratio)."""
        # Use Decimal math to apply % to a rupee value, then convert back to int.
        tax = D(self.subtotal_pkr) * self.tax_rate_ratio
        return _to_pkr_int(tax)

    @property
    def grand_total_pkr(self) -> int:
        """Canonical: grand total (PKR int)."""
        return int(self.subtotal_pkr) + int(self.tax_amount_pkr)

    @property
    def total_allocated_pkr(self) -> int:
        """
        Canonical: allocated/paid-to-this-order as PKR int.
        Assumes payment_allocations.amount is a money value (Decimal).
        """
        agg = self.payment_allocations.aggregate(s=Sum("amount"))
        return _to_pkr_int(agg["s"] or 0)

    @property
    def total_paid_pkr(self) -> int:
        """Alias for total_allocated_pkr."""
        return self.total_allocated_pkr

    @property
    def outstanding_balance_pkr(self) -> int:
        """Canonical: outstanding = grand_total_pkr - total_allocated_pkr."""
        return int(self.grand_total_pkr) - int(self.total_allocated_pkr)

    # ----------------- Display helpers (keep UI formatting in one place) -----------------
    @property
    def subtotal_display(self) -> str:
        return _pkr_str(self.subtotal_pkr)

    @property
    def tax_amount_display(self) -> str:
        return _pkr_str(self.tax_amount_pkr)

    @property
    def grand_total_display(self) -> str:
        return _pkr_str(self.grand_total_pkr)

    @property
    def total_allocated_display(self) -> str:
        return _pkr_str(self.total_allocated_pkr)

    @property
    def outstanding_balance_display(self) -> str:
        return _pkr_str(self.outstanding_balance_pkr)

    @property
    def produced_kg_display(self) -> str:
        return f"{dkg(self.produced_kg):,.3f}"

    @property
    def remaining_kg_display(self) -> str:
        return f"{dkg(self.remaining_kg):,.3f}"

    @property
    def billable_kg_display(self) -> str:
        return f"{dkg(self.billable_kg):,.3f}"

    # ----------------- Legacy Decimal properties (compat layer) -----------------
    # These keep your existing code working but are *derived* from the PKR-int truth.

    @property
    def subtotal(self) -> Decimal:
        """Legacy: Decimal rupees from PKR-int (no paise)."""
        return D(self.subtotal_pkr)

    @property
    def tax_amount(self) -> Decimal:
        return D(self.tax_amount_pkr)

    @property
    def grand_total(self) -> Decimal:
        return D(self.grand_total_pkr)

    @property
    def total_amount(self) -> int:
        """
        You previously used this as “alias for pre-tax subtotal”.
        Keep signature, but return PKR-int as int to match the new money standard.
        """
        return int(self.subtotal_pkr)

    @property
    def total_allocated(self) -> Decimal:
        return D(self.total_allocated_pkr)

    @property
    def total_paid(self) -> Decimal:
        return D(self.total_paid_pkr)

    @property
    def outstanding_balance(self) -> Decimal:
        return D(self.outstanding_balance_pkr)

    # ----------------- Validation (read-only) -----------------
    def clean(self):
        super().clean()

        if self._state.adding or not self.pk:
            return

        old_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()

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

        was_final = old_status in ("READY", "DELIVERED", "CLOSED")
        now_final = self.status in ("READY", "DELIVERED", "CLOSED")
        moving_to_final = (not was_final) and now_final
        if moving_to_final:
            produced = dkg(self.produced_kg)
            target = dkg(self.target_total_kg)
            diff = abs(produced - target)
            tol = dkg(self.tolerance_kg or D("0"))
            if diff > tol:
                raise ValidationError(
                    f"Produced {produced:,.3f} kg deviates from target {target:,.3f} kg by {diff:,.3f} kg. "
                    f"Tolerance: {self.tolerance_kg} kg."
                )

    # ----------------- Save (no deduction here; signals handle it) -----------------
    def save(self, *args, **kwargs):
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
