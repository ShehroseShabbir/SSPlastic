# core/ar_utils.py

from decimal import Decimal
from django.db import transaction
from .models_ar import Payment, PaymentAllocation
from .models import Order

def auto_apply_fifo(customer, limit_to_payment_ids=None):
    """
    Allocate customer's unapplied payments to oldest outstanding invoices (READY/DELIVERED/CLOSED).
    """
    outstanding = (
        Order.objects
        .filter(customer=customer, status__in=("READY","DELIVERED","CLOSED"))
        .order_by("delivery_date", "order_date", "id")
    )

    payments_qs = customer.payments_ar.all()
    if limit_to_payment_ids:
        payments_qs = payments_qs.filter(id__in=limit_to_payment_ids)

    with transaction.atomic():
        for p in payments_qs:
            remaining = p.unapplied_amount
            if remaining <= 0:
                continue

            for inv in outstanding:
                need = inv.outstanding_balance
                if need <= 0 or remaining <= 0:
                    continue

                alloc_amt = min(need, remaining)
                # Create or update a single allocation row per payment-order
                alloc, created = PaymentAllocation.objects.get_or_create(payment=p, order=inv, defaults={"amount": Decimal("0.00")})
                alloc.amount = (alloc.amount + alloc_amt).quantize(Decimal("0.01"))
                alloc.save()

                remaining = (remaining - alloc_amt).quantize(Decimal("0.01"))
                if remaining <= 0:
                    break
