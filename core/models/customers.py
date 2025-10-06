from decimal import Decimal
from django.db.models import Sum, Q, Value, Case, When, IntegerField, DecimalField
from django.db.models.functions import Coalesce
from django.db import models
from django.db.models import Sum, Q
from django.conf import settings
from core.models.common import money_int_pk
from core.utils_money import to_rupees_int
from django.apps import apps


# You already have FINAL_STATES in models_ar
from ..models_ar import FINAL_STATES


class Customer(models.Model):
    # --- Basic info ---
    company_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    country      = models.CharField(max_length=50, blank=True)
    phone        = models.CharField(max_length=20, blank=True)
    email        = models.EmailField(blank=True)
    address      = models.TextField(blank=True)

    # --- A/R: simple running balance ---
    previous_pending_balance_pkr = models.IntegerField(
        default=0,
        help_text="Initial carry-forward (once). In whole rupees."
    )
    pending_balance_pkr = models.IntegerField(
        default=0,
        help_text="Auto: carry + final orders − payments (int PKR)."
    )
    # Per-customer override (optional)
    charge_negative_dana = models.BooleanField(default=None, null=True, blank=True,
        help_text="Override global toggle: True/False. Leave blank to use Site Settings.")
    negative_dana_rate_pkr = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Override rate (PKR/kg). Leave blank to use Site Settings."
    )
    @property
    def carry_remaining_pkr(self) -> int:
        """
        Display-only: treat all positive payments as reducing carry-forward first.
        Returns max(carry − sum(positive payments), 0).
        """
        from core.models_ar import Payment
        from core.utils_money import to_rupees_int as _toint

        carry = int(self.previous_pending_balance_pkr or 0)
        # sum ONLY positive payments (ignore refunds which are negative)
        paid = 0
        for p in Payment.objects.filter(customer=self).only("amount"):
            n = _toint(p.amount)
            if n > 0:
                paid += n
        remaining = carry - paid
        return remaining if remaining > 0 else 0

    @property 
    def material_balance_kg(self) -> Decimal: 
        """ Net KG = sum of ledger deltas (IN minus OUT), quantified to 3 dp. """ 
        KG = DecimalField(max_digits=12, decimal_places=3) 
        agg = self.material_ledger.aggregate( 
            s=Coalesce(Sum("delta_kg", output_field=KG), 
                       Value(Decimal("0.000"), output_field=KG)) ) 
        val = agg["s"] or Decimal("0.000") # quantize to 3 dp (matches your UI) 
        return val.quantize(Decimal("0.001"))


    # def refresh_material_balance(self, save: bool = True) -> Decimal:
    #     """Recalculate from ledger and optionally persist to cached field."""
    #     KG = DecimalField(max_digits=12, decimal_places=3)
    #     agg = self.material_ledger.aggregate(
    #         s=Coalesce(Sum("delta_kg", output_field=KG), Value(Decimal("0.000"), output_field=KG))
    #     )
    #     val = agg["s"] or Decimal("0.000")
    #     if save:
    #         self.material_balance_kg_cached = val
    #         self.save(update_fields=["material_balance_kg_cached"])
    #     return val

    # ---- Pending balance (live compute fallback) ----
    @property
    def pending_balance_live_pkr(self) -> int:
        """
        Live calculation (no stored field needed):
        carry + sum(final orders) − sum(payments), in whole rupees.
        """
        FINAL_STATES = ("READY", "DELIVERED", "CLOSED")
        Order   = apps.get_model("core", "Order")
        Payment = apps.get_model("core", "Payment")

        # Sum of final/billable orders in PKR-int
        charges = sum(int(getattr(o, "grand_total_pkr", 0) or 0)
                      for o in Order.objects.filter(customer=self, status__in=FINAL_STATES))

        # Sum of all payments received (convert Decimal → int rupees)
        # Avoid importing helpers at module import time to prevent circulars.
        from core.utils_money import to_rupees_int as _toint
        payments = sum(_toint(p.amount) for p in Payment.objects.filter(customer=self))

        carry = int(self.previous_pending_balance_pkr or 0)
        return carry + int(charges) - int(payments)

    # ---- Convenience: persist the live value into the stored field ----
    def refresh_pending_balance(self, save: bool = True) -> int:
        """
        Recalculate and (optionally) store into pending_balance_pkr.
        Returns the new pending (int).
        """
        val = self.pending_balance_live_pkr
        if save:
            self.pending_balance_pkr = int(val)
            self.save(update_fields=["pending_balance_pkr"])
        return int(val)

    def __str__(self):
        return self.company_name