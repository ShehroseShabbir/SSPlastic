# core/material_sync.py
from decimal import Decimal
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import OrderItem  # or adjust import path
from .models import CustomerMaterialLedger, Order, MaterialReceipt
from .utils import dkg  # your Decimal quantizer

CONSUME_STATUSES = ("CONFIRMED", "INPROD", "READY", "DELIVERED", "SHIPPED", "CLOSED")

def _decimal_attr(obj, name, default="0"):
    """Read attribute; if callable, call; coerce to Decimal safely."""
    raw = getattr(obj, name, None)
    if callable(raw):
        raw = raw()
    try:
        return Decimal(str(raw if raw is not None else default))
    except Exception:
        return Decimal(default)

def sync_order_material_ledger(order: Order):
    """
    Keep exactly ONE OUT row per order that consumes material.
    - If status not in CONSUME_STATUSES: remove any OUT row (no deduction).
    - If status in CONSUME_STATUSES: create/update a single OUT row with -target kg.
    """
    consumes = order.status in CONSUME_STATUSES

    # how much to deduct (your “target” weight drives consumption)
    need = dkg(_decimal_attr(order, "target_total_kg", "0"))

    if not consumes or need <= 0:
        # Ensure no OUT for this order
        CustomerMaterialLedger.objects.filter(order=order, type="OUT").delete()
        return

    # Create/update a single OUT row (prevents double deduction when status changes)
    with transaction.atomic():
        CustomerMaterialLedger.objects.update_or_create(
            order=order,
            type="OUT",
            defaults={
                "customer": order.customer,
                "delta_kg": -need,  # negative = consumption
                "memo": f"Consumption for Order #{order.pk}",
            },
        )


@receiver(post_save, sender=MaterialReceipt)
def on_receipt_saved(sender, instance: MaterialReceipt, created, **kwargs):
    """Always keep one IN ledger row per receipt reflecting its total_kg."""
    total = dkg(_decimal_attr(instance, "total_kg", "0"))
    if not getattr(instance, "customer_id", None) or total <= 0:
        CustomerMaterialLedger.objects.filter(receipt=instance, type="IN").delete()
        return
    with transaction.atomic():
        CustomerMaterialLedger.objects.update_or_create(
            receipt=instance,
            customer=instance.customer,
            type="IN",
            defaults={"delta_kg": total, "memo": f"Receipt on {instance.date}"},
        )


@receiver(post_delete, sender=MaterialReceipt)
def on_receipt_deleted(sender, instance: MaterialReceipt, **kwargs):
    CustomerMaterialLedger.objects.filter(receipt=instance, type="IN").delete()
    # Remove ALL ledger rows tied to this order (typically the single OUT row)
    CustomerMaterialLedger.objects.filter(order=instance).delete()

@receiver(post_save, sender=Order)
def on_order_saved(sender, instance: Order, **kwargs):
    # Re-sync whenever an order changes (status or target weight)
    sync_order_material_ledger(instance)

@receiver(post_delete, sender=OrderItem)
def on_item_deleted(sender, instance, **kwargs):
    sync_order_material_ledger(instance.order)