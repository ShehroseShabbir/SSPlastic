# core/material_sync.py
from decimal import Decimal
from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from core.utils_weight import dkg  # Decimal quantizer to 3dp

# Statuses that should CONSUME material (create/keep one OUT row).
CONSUME_STATUSES = ("CONFIRMED", "INPROD", "READY", "DELIVERED", "CLOSED")

# ----- Lazy model getters (avoid circular imports) -----
def Order():
    return apps.get_model("core", "Order")

def OrderItem():
    return apps.get_model("core", "OrderItem")

def MaterialReceipt():
    return apps.get_model("core", "MaterialReceipt")

def Ledger():
    return apps.get_model("core", "CustomerMaterialLedger")


# ----- Helpers -----
def _as_dec(val, default="0"):
    try:
        return Decimal(str(val if val is not None else default))
    except Exception:
        return Decimal(default)


# ----- Core sync: keep exactly ONE OUT row per order -----
def sync_order_material_ledger(order):
    """
    - If order status not in CONSUME_STATUSES or target <= 0: remove OUT row.
    - Else create/update ONE OUT row with customer, memo, and -target_total_kg.
    """
    L = Ledger()
    consumes = getattr(order, "status", None) in CONSUME_STATUSES
    need = dkg(_as_dec(getattr(order, "target_total_kg", 0)))

    if (not consumes) or need <= 0:
        L.objects.filter(order=order, type="OUT").delete()
        return

    with transaction.atomic():
        L.objects.update_or_create(
            order=order,
            type="OUT",
            defaults={
                "customer": order.customer,
                "delta_kg": -need,  # negative = consumption
                "memo": f"Consumption for Order {getattr(order, 'invoice_number', order.pk)}",
            },
        )


# ----- Signals -----
@receiver(post_save, sender=Order())
def _order_saved(sender, instance, **kwargs):
    # Re-sync material deduction when order changes (status/target/etc.)
    sync_order_material_ledger(instance)


@receiver(post_delete, sender=Order())
def _order_deleted(sender, instance, **kwargs):
    # Clean any ledger rows tied to this order (e.g., the single OUT row)
    Ledger().objects.filter(order=instance).delete()


@receiver(post_save, sender=MaterialReceipt())
def _receipt_saved(sender, instance, created, **kwargs):
    """
    Keep exactly ONE IN row per receipt reflecting its total kg.
    Delete the IN row if customer missing or total <= 0.
    """
    L = Ledger()
    total = dkg(_as_dec(getattr(instance, "total_kg", 0)))
    if not getattr(instance, "customer_id", None) or total <= 0:
        L.objects.filter(receipt=instance).delete()
        return

    with transaction.atomic():
        L.objects.update_or_create(
            receipt=instance,                  # key on receipt → one IN row per receipt
            defaults={
                "customer": instance.customer,
                "type": "IN",
                "material_type": getattr(instance, "material_type", "") or "",
                "delta_kg": total,
                "memo": f"Receipt on {getattr(instance, 'date', '')}",
            },
        )


@receiver(post_delete, sender=MaterialReceipt())
def _receipt_deleted(sender, instance, **kwargs):
    # Remove ALL ledger rows tied to this receipt (prevents ghosts & delete-time errors)
    Ledger().objects.filter(receipt=instance).delete()


@receiver(post_delete, sender=OrderItem())
def _order_item_deleted(sender, instance, **kwargs):
    # If deleting an item affects target_total_kg via your own logic, resync the order.
    order = getattr(instance, "order", None)
    if order:
        sync_order_material_ledger(order)