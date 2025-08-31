# core/ar_simple.py
from django.apps import apps
from django.db import transaction
from django.db.models import Sum
from core.utils_money import to_rupees_int as _toint

FINAL_STATES = ("READY","DELIVERED","CLOSED")

def _Order():            return apps.get_model("core","Order")
def _Payment():          return apps.get_model("core","Payment")
def _PaymentAllocation():return apps.get_model("core","PaymentAllocation")
def _Customer():         return apps.get_model("core","Customer")

def refresh_customer_pending(customer_id: int):
    """pending = carry + sum(final orders) − sum(payments). No monthly complexity."""
    Order = _Order(); Payment = _Payment(); Customer = _Customer()
    c = Customer.objects.get(pk=customer_id)
    charges = sum(int(getattr(o,"grand_total_pkr",0) or 0)
                  for o in Order.objects.filter(customer=c, status__in=FINAL_STATES))
    payments = sum(_toint(p.amount) for p in Payment.objects.filter(customer=c))
    c.pending_balance_pkr = int(c.previous_pending_balance_pkr or 0) + int(charges) - int(payments)
    c.save(update_fields=["pending_balance_pkr"])

def _orders_with_outstanding(customer):
    """Oldest first, only final orders with >0 outstanding."""
    Order = _Order()
    orders = (Order.objects
              .filter(customer=customer, status__in=FINAL_STATES)
              .order_by("order_date","id"))
    return [o for o in orders if int(getattr(o,"outstanding_balance_pkr",0) or 0) > 0]

def auto_allocate_payment(payment_id: int, *, reset_existing=True):
    """
    FIFO allocate a payment to oldest unpaid orders.
    - If reset_existing=True, clear this payment's allocations first (keeps it idempotent).
    - Uses 'amount' only (no rounding for simplicity).
    """
    Payment = _Payment(); PaymentAllocation = _PaymentAllocation()
    p = Payment.objects.select_related("customer").get(pk=payment_id)

    with transaction.atomic():
        if reset_existing:
            PaymentAllocation.objects.filter(payment=p).delete()

        remaining = _toint(p.amount)
        if remaining <= 0:
            return

        for o in _orders_with_outstanding(p.customer):
            need = int(getattr(o,"outstanding_balance_pkr",0) or 0)
            if need <= 0:
                continue
            alloc = min(remaining, need)
            if alloc <= 0:
                break
            PaymentAllocation.objects.create(payment=p, order=o, amount=int(alloc))
            remaining -= alloc
            if remaining <= 0:
                break

def allocate_unapplied_for_customer(customer_id: int):
    """
    If there are payments with room to allocate (after edits), allocate them FIFO.
    We treat 'room' as the payment.amount minus sum of its allocations.
    """
    Payment = _Payment(); PaymentAllocation = _PaymentAllocation(); Customer = _Customer()
    c = Customer.objects.get(pk=customer_id)
    for p in Payment.objects.filter(customer=c):
        allocated = int(PaymentAllocation.objects.filter(payment=p).aggregate(s=Sum("amount"))["s"] or 0)
        cap = _toint(p.amount) - allocated
        if cap > 0:
            auto_allocate_payment(p.pk, reset_existing=False)
