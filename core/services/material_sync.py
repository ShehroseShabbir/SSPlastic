# core/material_sync.py
from decimal import Decimal
from datetime import datetime, time

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone

from core.utils_weight import dkg  # Decimal quantizer to 3dp

# We import the Ledger model directly (no circular); the others via apps.get_model
from core.models.materials import CustomerMaterialLedger


# Orders in these statuses "consume" material (create/keep one OUT row).
CONSUME_STATUSES = ("CONFIRMED", "INPROD", "READY", "DELIVERED", "CLOSED")


# ----- Lazy model getters (avoid circular imports at import time) -----
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


def _aware_midnight(d):
    """
    Convert a date (or date-like) to an aware datetime (local tz) at 00:00.
    """
    if not d:
        return timezone.now()
    dt = datetime.combine(d, time(0, 0, 0))
    if timezone.is_aware(dt):
        return dt
    return timezone.make_aware(dt)


# ----- Core sync: keep exactly ONE OUT row per order -----
def sync_order_material_ledger(order):
    """
    - If order.status not in CONSUME_STATUSES or target <= 0: remove OUT row.
    - Else create/update ONE OUT row:
        * customer = order.customer
        * delta_kg = -target_total_kg  (negative means consumption)
        * date     = order.order_date (00:00) if present, else now()
        * memo     = "Consumption for Order <invoice or pk>"
    """
    L = Ledger()
    consumes = getattr(order, "status", None) in CONSUME_STATUSES
    need = dkg(_as_dec(getattr(order, "target_total_kg", 0)))

    if (not consumes) or need <= 0:
        L.objects.filter(order=order, type="OUT").delete()
        return

    # choose a meaningful timestamp for month cutoffs
    order_dt = _aware_midnight(getattr(order, "order_date", None))

    with transaction.atomic():
        L.objects.update_or_create(
            order=order,
            type="OUT",
            defaults={
                "customer": order.customer,
                "delta_kg": -need,  # negative = consumption
                "memo": f"Consumption for Order {getattr(order, 'invoice_number', order.pk)}",
                "date": order_dt,   # requires CustomerMaterialLedger.date to be settable (not auto_now_add)
            },
        )


# ----- Signals for Order -----
@receiver(post_save, sender=Order())
def _order_saved(sender, instance, **kwargs):
    # Re-sync material deduction when order changes (status/target/etc.)
    sync_order_material_ledger(instance)


@receiver(post_delete, sender=Order())
def _order_deleted(sender, instance, **kwargs):
    # Clean any ledger rows tied to this order (e.g., the single OUT row)
    Ledger().objects.filter(order=instance).delete()


# ----- Signals for MaterialReceipt → Ledger (1:1) -----
@receiver(post_save, sender=MaterialReceipt())
def _receipt_saved(sender, instance, created, **kwargs):
    """
    Keep exactly ONE ledger row per receipt reflecting its signed total kg.

    Behavior:
      - If total_kg > 0: create/update an IN entry (+total_kg)
      - If total_kg < 0: create/update an OUT entry (delta_kg is negative)
      - If customer missing OR total_kg == 0: delete any linked ledger entry

    The ledger 'date' mirrors the receipt 'date' (00:00, TZ-aware) so
    month statements align with the business date of the receipt.
    """
    L = Ledger()
    total = dkg(_as_dec(getattr(instance, "total_kg", 0)))

    # If no customer or zero qty, remove any existing linked entry
    if not getattr(instance, "customer_id", None) or total == 0:
        L.objects.filter(receipt=instance).delete()
        return

    entry_type = CustomerMaterialLedger.EntryType.OUT if total < 0 else CustomerMaterialLedger.EntryType.IN
    # keep the sign in delta_kg; OUT will be negative
    delta = total

    ledger_dt = _aware_midnight(getattr(instance, "date", None))

    with transaction.atomic():
        L.objects.update_or_create(
            receipt=instance,  # 1:1 mapping
            defaults={
                "customer": instance.customer,
                "order": None,
                "material_type": getattr(instance, "material_type", "") or "",
                "type": entry_type,
                "delta_kg": delta,
                "memo": (getattr(instance, "notes", "") or "").strip(),
                "date": ledger_dt,  # set from receipt date
            },
        )


@receiver(post_delete, sender=MaterialReceipt())
def _receipt_deleted(sender, instance, **kwargs):
    # Remove ALL ledger rows tied to this receipt
    Ledger().objects.filter(receipt=instance).delete()


@receiver(post_delete, sender=OrderItem())
def _order_item_deleted(sender, instance, **kwargs):
    # If deleting an item affects target_total_kg via your logic, resync the order.
    order = getattr(instance, "order", None)
    if order:
        sync_order_material_ledger(order)




# --- (Optional) Cached customer material balance ---
# If you REMOVED the cached balance on Customer (material_balance_kg_cached),
# delete the receiver below entirely. If you still have the cache and want to
# keep it in sync, leave the receiver and ensure the Customer method exists.
#
# from core.models.materials import CustomerMaterialLedger
# @receiver([post_save, post_delete], sender=CustomerMaterialLedger)
# def _sync_customer_material_cache(sender, instance, **kwargs):
#     # Any change in ledger -> refresh customer cached balance
#     # Only keep this if you still use customer.refresh_material_balance(...)
#     instance.customer.refresh_material_balance(save=True)
