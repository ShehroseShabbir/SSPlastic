from decimal import Decimal
from django.db.models import Sum, Q, Value, Case, When, IntegerField, DecimalField
from django.db.models.functions import Coalesce
from django.db import models
from django.db.models import Sum, Q
from django.conf import settings
from core.models.common import money_int_pk
from core.utils_money import to_rupees_int
from django_countries.fields import CountryField

# You already have FINAL_STATES in models_ar
from ..models_ar import FINAL_STATES


class Customer(models.Model):
    company_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    country = CountryField()  # or CountryField if using django-countries
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    previous_pending_balance_pkr = models.IntegerField(
        default=0,
        help_text="Carry-forward A/R from previous month in whole rupees (PKR)."
    )
    @property
    def ar_invoices_total(self) -> Decimal:
        total = Decimal("0.00")
        qs = self.orders.filter(status__in=FINAL_STATES).select_related("customer").prefetch_related("rolls")
        for o in qs:
            total += (o.grand_total or Decimal("0.00"))
        return total.quantize(Decimal("0.01"))

    @property
    def ar_allocations_total(self) -> Decimal:
        from ..models_ar import PaymentAllocation
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
        agg = self.material_ledger.aggregate(s=Sum('delta_kg'))
        return agg['s'] or Decimal('0')

      # ---------- NEW: Computed totals (properties) ----------
    @property
    def total_material_kg(self) -> Decimal:
        """
        Sum of ALL material ledger entries (IN minus OUT) = current balance (kg).
        Mirrors material_balance_kg, but explicitly typed and 3dp.
        """
        agg = self.material_ledger.aggregate(
            s=Coalesce(Sum("delta_kg", output_field=DecimalField(max_digits=12, decimal_places=3)),
                       Value(Decimal("0.000"), output_field=DecimalField(max_digits=12, decimal_places=3)))
        )
        return agg["s"] or Decimal("0.000")

    @property
    def total_material_lifetime_kg(self) -> Decimal:
        """
        Sum of IN entries only over lifetime (kg).
        """
        agg = self.material_ledger.filter(delta_kg__gt=0).aggregate(
            s=Coalesce(Sum("delta_kg", output_field=DecimalField(max_digits=12, decimal_places=3)),
                       Value(Decimal("0.000"), output_field=DecimalField(max_digits=12, decimal_places=3)))
        )
        return agg["s"] or Decimal("0.000")

    @property
    def total_remaining_kg(self) -> Decimal:
        """
        Remaining (target - produced) across NON-final orders.
        Uses the Python property Order.remaining_kg (safe).
        """
        total = Decimal("0.000")
        # Consider these "open" for remaining: DRAFT/CONFIRMED/INPROD/READY
        open_statuses = ("DRAFT", "CONFIRMED", "INPROD", "READY")
        for o in self.orders.only("id", "status", "target_total_kg").filter(status__in=open_statuses):
            try:
                total += (o.remaining_kg or Decimal("0.000"))
            except Exception:
                pass
        return total
    # ---------- NEW: Carry-forward aware pending (rupees int) ----------
    @property
    def ar_pending_balance_pkr(self) -> int:
        """
        ar_pending_balance is Decimal(2dp). Convert to whole rupees (int).
        """
        try:
            return to_rupees_int(self.ar_pending_balance)
        except Exception:
            return 0

    @property
    def ar_total_due_with_carry_pkr(self) -> int:
        """
        Current pending (int rupees) + carry-forward (int rupees).
        """
        return int(self.ar_pending_balance_pkr) + int(self.previous_pending_balance_pkr)

    # ---------- Optional helper setter ----------
    def set_previous_pending_balance(self, amount) -> None:
        """
        Store carry-forward as int rupees (accepts int/Decimal/str).
        """
        self.previous_pending_balance_pkr = money_int_pk(to_rupees_int(amount))
        self.save(update_fields=["previous_pending_balance_pkr"])

    def __str__(self):
        return self.company_name
