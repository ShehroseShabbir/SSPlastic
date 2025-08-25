# core/models_ar.py  (or keep inside your existing models.py)

from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Sum, Value, IntegerField, F
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.models.common import BANK_NAMES

from .utils_money import D, to_rupees_int, money_mul, round_to
from .utils_weight import dkg 

PAYMENT_METHODS = [
    ("CASH", "Cash"),
    ("CHEQUE", "Cheque"),
    ("TRANSFER", "Bank Transfer"),
    ("OTHER", "Other"),
]

FINAL_STATES = ("READY", "DELIVERED", "CLOSED")  # billable/collectable
SMALL_WRITE_OFF_MAX = 100
class Payment(models.Model):
    customer = models.ForeignKey("Customer", on_delete=models.CASCADE, related_name="payments_ar")
    received_on = models.DateField(default=timezone.now)
    method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default="TRANSFER")
    bank = models.CharField(choices=BANK_NAMES, blank=True, max_length=50)
    reference = models.CharField(max_length=64, blank=True)  # cheque no., bank ref, etc.
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # positive for receipt; negative for refund
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_on", "-id"]
        verbose_name = "Receive Payment"
        verbose_name_plural = "Receive Payments"

    def __str__(self):
        return f"{self.received_on} – Unapplied Rs. {self.unapplied_amount:,.2f} PKR / Total Rs. {self.amount:,.2f} PKR – {self.customer}"

    @property
    def allocated_amount(self) -> Decimal:
        agg = self.allocations.aggregate(s=Sum("amount"))
        return round_to(agg["s"] or 0)

    @property
    def unapplied_amount(self) -> Decimal:
        return round_to(self.amount - self.allocated_amount)

    def clean(self):
        super().clean()
        # Defensive: prevent negative “unapplied” via allocations elsewhere
        if self.pk and self.unapplied_amount < Decimal("0"):
            raise ValidationError("Payment is over-allocated (negative unapplied). Reduce allocations.")

class PaymentAllocation(models.Model):
    payment = models.ForeignKey("Payment", on_delete=models.CASCADE, related_name="allocations")
    order   = models.ForeignKey("core.Order", on_delete=models.CASCADE, related_name="payment_allocations")

    # PKR integers
    amount       = models.BigIntegerField(default=0)
    applied_on   = models.DateField(default=timezone.now)
    rounding_pkr = models.IntegerField(
        default=0,
        help_text="Small +/- write-off used to settle the order (± few rupees).",
    )

    class Meta:
        unique_together = [("payment", "order")]
        verbose_name = "Allocate Payment"
        verbose_name_plural = "Allocate Payments"

    def __str__(self):
        inv = getattr(self.order, "invoice_number", self.order_id)
        return f"Alloc Rs {int(self.amount):,} of Payment {self.payment_id} → Order {inv}"

    @property
    def effective_amount(self) -> int:
        """What this allocation counts toward settling the order (cash + rounding)."""
        return int(self.amount or 0) + int(self.rounding_pkr or 0)

    def clean(self):
        super().clean()

        amt = int(self.amount or 0)
        rnd = int(self.rounding_pkr or 0)

        if amt <= 0:
            raise ValidationError("Allocation amount must be a positive PKR integer.")

        if abs(rnd) > SMALL_WRITE_OFF_MAX:
            raise ValidationError(
                f"rounding_pkr must be within ±Rs {SMALL_WRITE_OFF_MAX} (you entered {rnd})."
            )

        # Only allocate to final/billable orders
        if getattr(self.order, "status", None) not in FINAL_STATES:
            raise ValidationError("Cannot allocate to a non-final (not yet billable) order.")

        # ---- (1) Cash guard (ignore rounding here) ----
        other_cash = (
            self.payment.allocations.exclude(pk=self.pk)
            .aggregate(
                s=Coalesce(Sum("amount", output_field=IntegerField()), Value(0, output_field=IntegerField()))
            )["s"] or 0
        )
        unapplied_cash = int(self.payment.amount or 0) - int(other_cash)
        if amt > unapplied_cash:
            raise ValidationError(
                f"Cash exceeds payment's unapplied: trying Rs {amt:,}, "
                f"unapplied Rs {max(unapplied_cash, 0):,}."
            )

        # ---- (2) Effective guard vs. outstanding (includes rounding) ----
        # Sum of other allocations' effective amounts (amount + rounding_pkr)
        other_effective = (
            self.order.payment_allocations.exclude(pk=self.pk)
            .aggregate(
                s=Coalesce(
                    Sum(F("amount") + F("rounding_pkr"), output_field=IntegerField()),
                    Value(0, output_field=IntegerField()),
                )
            )["s"] or 0
        )

        # Expect Order to expose PKR-int grand total. Fallback to 0 if missing.
        order_total_int = int(getattr(self.order, "grand_total_pkr", 0))
        outstanding_now = order_total_int - int(other_effective)

        effective = amt + rnd
        tolerance = SMALL_WRITE_OFF_MAX

        if effective < 0:
            raise ValidationError("Effective allocation cannot be negative.")

        # allow up to outstanding + tolerance
        if effective > outstanding_now + tolerance:
            over_by = effective - outstanding_now
            raise ValidationError(
                "This allocation would overpay the order by more than the allowed tolerance "
                f"(±Rs {tolerance}). Details: outstanding Rs {max(outstanding_now,0):,}, "
                f"effective Rs {effective:,}, over by Rs {over_by:,}. "
                f"Tip: reduce amount or set rounding_pkr to a small negative within ±Rs {tolerance}."
            )