# core/utils_billing.py

from datetime import date
from decimal import Decimal

from django.db.models import Sum, IntegerField, DecimalField
from django.db.models.functions import Coalesce
from django.db.models import Value
from django.utils import timezone

from core.models import Order, CustomerMaterialLedger
from core.models_ar import Payment, FINAL_STATES
from core.utils_money import D, round_to
from core.utils_weight import dkg
from core.utils import _neg_dana_config, get_site_settings


def compute_customer_balance_as_of(customer, as_of=None):
    """
    LIVE BALANCE: closing_due_pkr using the exact same billing math
    as your PDF logic, but up to `as_of` (default = today).

    This is NOT range-based. It always considers full history.
    """
    if as_of is None:
        as_of = timezone.localdate()

    # ---- 1) Carry-forward ----
    carry = int(customer.previous_pending_balance_pkr or 0)

    # ---- 2) Orders up to date ----
    charges_total = 0
    orders_qs = Order.objects.filter(
        customer=customer,
        status__in=FINAL_STATES,
        order_date__lte=as_of,
    )

    for o in orders_qs:
        # the same logic used in your PDF generator
        charges_total += int(getattr(o, "grand_total_pkr", 0) or 0)


    # ---- 3) Payments up to date ----
    payments_total = (
        Payment.objects.filter(
            customer=customer,
            received_on__lte=as_of,
        )
        .aggregate(
            s=Coalesce(Sum("amount", output_field=IntegerField()),
                       Value(0, output_field=IntegerField()))
        )["s"]
        or 0
    )

    # ---- 4) Negative Dana up to date ----
    ss = get_site_settings()
    neg_enabled, neg_rate, neg_label = _neg_dana_config(customer, ss)

    KG0 = Decimal("0.000")

    closing_kg = (
        CustomerMaterialLedger.objects.filter(customer=customer, date__lte=as_of)
        .aggregate(
            v=Coalesce(
                Sum("delta_kg", output_field=DecimalField(max_digits=12, decimal_places=3)),
                Value(KG0, output_field=DecimalField(max_digits=12, decimal_places=3)),
            )
        )["v"]
        or KG0
    )

    shortfall_kg = dkg(-closing_kg) if closing_kg < 0 else Decimal("0.000")

    neg_dana_charge = 0
    if neg_enabled and shortfall_kg > 0 and neg_rate > 0:
        neg_dana_charge = int(round_to(shortfall_kg * neg_rate, 0))

    # ---- 5) Final live balance ----
    closing_due = carry + charges_total + neg_dana_charge - payments_total
    return int(closing_due)
