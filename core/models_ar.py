# core/models_ar.py  (or keep inside your existing models.py)

from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from django.utils import timezone

PAYMENT_METHODS = [
    ("CASH", "Cash"),
    ("CHEQUE", "Cheque"),
    ("TRANSFER", "Bank Transfer"),
    ("OTHER", "Other"),
]

FINAL_STATES = ("READY", "DELIVERED", "CLOSED")  # billable/collectable

def dq2(x):
    return (Decimal(x or 0)).quantize(Decimal("0.01"))

class Payment(models.Model):
    customer = models.ForeignKey("Customer", on_delete=models.CASCADE, related_name="payments_ar")
    received_on = models.DateField(default=timezone.now)
    method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default="TRANSFER")
    reference = models.CharField(max_length=64, blank=True)  # cheque no., bank ref, etc.
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # positive for receipt; negative for refund
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_on", "-id"]

    def __str__(self):
        return f"Pmt #{self.id} {self.received_on} – Unapplied ${self.unapplied_amount:,.2f} / Total ${self.amount:,.2f} – {self.customer}"

    @property
    def allocated_amount(self) -> Decimal:
        agg = self.allocations.aggregate(s=Sum("amount"))
        return dq2(agg["s"] or 0)

    @property
    def unapplied_amount(self) -> Decimal:
        return dq2(self.amount - self.allocated_amount)

    def clean(self):
        super().clean()
        # Defensive: prevent negative “unapplied” via allocations elsewhere
        if self.pk and self.unapplied_amount < Decimal("0"):
            raise ValidationError("Payment is over-allocated (negative unapplied). Reduce allocations.")

class PaymentAllocation(models.Model):
    """
    A portion of a Payment applied to a specific Order (invoice).
    """
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # >= 0.01 typical
    applied_on = models.DateField(default=timezone.now)

    class Meta:
        unique_together = [("payment", "order")]  # one row per payment-order pair is usually clean

    def __str__(self):
        return f"Alloc ${self.amount:,.2f} of Pmt {self.payment_id} → Order {self.order.invoice_number}"

    def clean(self):
        super().clean()
        if self.amount <= 0:
            raise ValidationError("Allocation amount must be positive.")

        # 1) Cannot allocate more than payment.unapplied (excluding *this* row)
        other_allocs = self.payment.allocations.exclude(pk=self.pk).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        max_remaining = dq2(self.payment.amount - other_allocs)
        if self.amount > max_remaining:
            raise ValidationError(
                f"Allocation Rs. {self.amount:,.2f} PKR exceeds payment's unapplied Rs. {max_remaining:,.2f} PKR."
            )

        # 2) Optional: prevent allocating to non-billable orders (enforce only when moving final → your call)
        if self.order.status not in FINAL_STATES:
            raise ValidationError("Cannot allocate to a non-final (not yet billable) order.")

        # 3) Cannot allocate more than order’s current outstanding (excluding this row)
        other_on_order = self.order.payment_allocations.exclude(pk=self.pk).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        outstanding_now = dq2(self.order.grand_total - other_on_order)
        if self.amount > outstanding_now:
            raise ValidationError(
                f"Allocation ${self.amount:,.2f} exceeds order outstanding ${outstanding_now:,.2f}."
            )
