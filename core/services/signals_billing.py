# core/signals_billing.py
from __future__ import annotations
from django.db.models.signals import post_save, post_delete
from django.db import transaction
from django.apps import apps



# # ---------- (A) Orders → Statement lines ----------
# @receiver(post_save, sender=Order)
# def _order_to_statement(sender, instance: Order, created, **kwargs):
#     """
#     If Order is in a FINAL state, ensure a statement line exists in that order's month.
#     If Order is not final, remove any statement line for it.
#     """
#     if getattr(instance, "invoice_number", None) is None:
#         # ensure it has an ID/number first (you already set it in save())
#         return

#     # Determine month to bill: use order_date (common for AR). Adjust if you prefer delivery date.
#     bill_date = instance.order_date or timezone.now().date()
#     st = get_or_create_statement(instance.customer, bill_date)

#     # Hard stop if user has frozen the statement (they re-issued it)
#     if st.frozen:
#         return

#     # Remove any existing line if order is not final
#     if instance.status not in FINAL_STATES:
#         StatementLine.objects.filter(statement__customer=instance.customer, order=instance).delete()
#         st.recompute()
#         return

#     # Upsert line
#     defaults = dict(
#         line_type="ORDER",
#         date=bill_date,
#         description=f"Order {instance.invoice_number}",
#         qty_kg=instance.billable_kg,
#         rate=instance.price_per_kg,
#         amount_pkr=int(instance.grand_total_pkr or 0),
#     )
#     obj, created_line = StatementLine.objects.update_or_create(
#         statement=st,
#         order=instance,
#         defaults=defaults,
#     )
#     st.recompute()
#     allocate_unapplied_to_order(instance)

# @receiver(post_save, sender=StatementLine)
# def _line_saved(sender, instance, **kwargs):
#     # Avoid loop: the override line is created/updated inside recompute itself
#     if instance.line_type == "ADJ" and instance.description == CLOSING_OVERRIDE_DESC:
#         return
#     instance.statement.recompute(save=True)

# @receiver(post_delete, sender=StatementLine)
# def _line_deleted(sender, instance, **kwargs):
#     # Deleting the override line should recompute once (no loop here)
#     instance.statement.recompute(save=True)

# # ---------- (B) Payments → Statement line + FIFO allocation ----------
# def _apply_payment_fifo(payment: Payment):
#     """
#     Apply payment.unapplied_amount to oldest outstanding FINAL orders (FIFO).
#     Uses PKR-integers for allocations.
#     """
#     from decimal import Decimal
#     cash_left = int(Decimal(str(payment.unapplied_amount or 0)).quantize(Decimal("1")))  # whole rupees

#     if cash_left <= 0:
#         return

#     # Oldest first = by order_date then id
#     orders = (
#         Order.objects.filter(customer=payment.customer, status__in=FINAL_STATES)
#         .order_by("order_date", "id")
#         .all()
#     )

#     for o in orders:
#         if cash_left <= 0:
#             break

#         # Outstanding in PKR-int
#         # effective allocated so far on this order
#         other_effective = (
#             o.payment_allocations.aggregate(
#                 s=Coalesce(Sum(F("amount") + F("rounding_pkr"), output_field=IntegerField()), 0)
#             )["s"] or 0
#         )
#         outstanding = int(o.grand_total_pkr) - int(other_effective)
#         if outstanding <= 0:
#             continue

#         alloc_amt = min(outstanding, cash_left)
#         if alloc_amt <= 0:
#             continue

#         # Create or update one allocation row per (payment, order)
#         pa, _ = PaymentAllocation.objects.get_or_create(payment=payment, order=o, defaults={"amount": 0})
#         pa.amount = int(pa.amount or 0) + int(alloc_amt)
#         pa.full_clean()
#         pa.save()

#         cash_left -= alloc_amt
#     # Done; any remainder stays as unapplied (will show as credit next month too)


# @receiver(post_save, sender=Payment)
# def _payment_to_statement(sender, instance: Payment, created, **kwargs):
#     """
#     Every payment creates a negative 'PAY' line in the month it was received.
#     Then we auto-apply FIFO to orders.
#     """
#     st = get_or_create_statement(instance.customer, instance.received_on)

#     if not st.frozen:
#         StatementLine.objects.update_or_create(
#             statement=st,
#             payment=instance,
#             defaults=dict(
#                 line_type="PAY",
#                 date=instance.received_on,
#                 description=f"Payment ({instance.get_method_display()}) {instance.reference or ''}".strip(),
#                 amount_pkr= -abs(int(Decimal(str(instance.amount)).quantize(Decimal('1')))),
#             ),
#         )
#         st.recompute()

#     # Auto-apply FIFO to orders
#     _apply_payment_fifo(instance)


# @receiver(post_delete, sender=Payment)
# def _payment_line_delete(sender, instance: Payment, **kwargs):
#     StatementLine.objects.filter(payment=instance).delete()
#     # Recompute affected month if present
#     try:
#         st = MonthlyStatement.objects.get(customer=instance.customer,
#                                           year=instance.received_on.year, month=instance.received_on.month)
#         st.recompute()
#     except MonthlyStatement.DoesNotExist:
#         pass

# def allocate_unapplied_to_order(order):
#     Payment = apps.get_model("core", "Payment")
#     PaymentAllocation = apps.get_model("core", "PaymentAllocation")

#     # outstanding for this order (in PKR-int, includes any previous rounding)
#     agg = order.payment_allocations.aggregate(
#         s=Coalesce(Sum(F("amount") + F("rounding_pkr"), output_field=IntegerField()), 0)
#     )
#     already = int(agg["s"] or 0)
#     need = int(order.grand_total_pkr) - already
#     if need <= 0:
#         return

#     # oldest credits first
#     for p in Payment.objects.filter(customer=order.customer).order_by("received_on", "id"):
#         # whole-rupees unapplied on this payment
#         cash_left = int(Decimal(str(p.unapplied_amount or 0)).quantize(Decimal("1")))
#         if cash_left <= 0:
#             continue

#         alloc_amt = min(need, cash_left)
#         pa, _ = PaymentAllocation.objects.get_or_create(payment=p, order=order, defaults={"amount": 0})
#         pa.amount = int(pa.amount or 0) + int(alloc_amt)
#         pa.full_clean()
#         pa.save()

#         need -= alloc_amt
#         if need <= 0:
#             break

def propagate_carry_forward(statement):
    """
    Copy this statement's closing into the customer's carry-forward field.
    Call this when the month is CLOSED/FROZEN.
    """
    if statement is None or statement.customer_id is None:
        return
    with transaction.atomic():
        type(statement.customer).objects.filter(pk=statement.customer_id)\
            .update(previous_pending_balance_pkr=int(statement.closing_pkr or 0))