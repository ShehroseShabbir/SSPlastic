# core/models/raw_material.py
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.core.validators import MinValueValidator
from django.db.models import Sum

from core.models.customers import Customer
from core.models.materials import CustomerMaterialLedger  # ← adjust if your path differs
from core.models.common import BANK_NAMES  # for supplier payment bank choices
from core.utils_weight import dkg  # returns Decimal with 3dp

BAG_WEIGHT_KG = Decimal("25.000")

# Keep methods simple (no import from AR payments to avoid mixing A/R & A/P)
SUPPLIER_PAYMENT_METHODS = [
    ("CASH", "Cash"),
    ("CHEQUE", "Cheque"),
    ("TRANSFER", "Bank Transfer"),
    ("OTHER", "Other"),
]


def _ledger_dt_for(date_field):
    """
    Use the txn 'when' (DateField) as the ledger DateTime so PDF/month filters
    align with your chosen period. Noon avoids DST edges.
    """
    naive = datetime.combine(date_field, time(12, 0, 0))
    tz = timezone.get_current_timezone()
    return timezone.make_aware(naive, tz) if timezone.is_naive(naive) else naive


class RawMaterialTxn(models.Model):
    """
    Auditable record of raw-material movements & commercial terms.
    Money in whole PKR; quantity in kg (Decimal 3dp).
    All kg effects are written to CustomerMaterialLedger via .apply() atomically.
    """

    class Kind(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchase (into Company Stock)"
        SALE     = "SALE",     "Sell from Company Stock to Customer"
        TRANSFER = "TRANSFER", "Customer → Customer transfer"

    class MaterialKind(models.TextChoices):
        FILM = "FILM", "Film"
        TAPE = "TAPE", "Tape"

    # Core attributes
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PURCHASE)
    when = models.DateField(default=timezone.localdate)

    # Parties
    supplier_name = models.CharField(max_length=200, blank=True)  # for PURCHASE
    from_customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT, related_name="rm_out_txns"
    )
    to_customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT, related_name="rm_in_txns"
    )

    # Quantities & money
    qty_kg = models.DecimalField(
        max_digits=12, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))]
    )
    # allow decimals in rate (e.g., 52.75 PKR/kg)
    rate_pkr = models.DecimalField(
        max_digits=12, decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Rate per kg (PKR, can include decimals)."
    )
    amount_pkr = models.DecimalField(
        max_digits=12, decimal_places=2,
        default=Decimal("0.00"), 
        validators=[MinValueValidator(0)], 
        help_text="Total amount in PKR."
    )

    # Extra capture for your workflow
    material_type = models.CharField(max_length=10, choices=MaterialKind.choices, default=MaterialKind.FILM)
    bags_count = models.PositiveIntegerField(default=0, help_text="Each bag = 25 kg.")
    dc_number = models.CharField(max_length=50, blank=True, help_text="Delivery challan / reference.")
    memo = models.CharField(max_length=255, blank=True)

    # Audit
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="rm_created"
    )

    class Meta:
        permissions = [
            ("can_manage_material_trades", "Can manage raw-material purchases/sales/transfers"),
        ]
        ordering = ["-when", "-id"]
        verbose_name = "Raw Material Transaction"
        verbose_name_plural = "Raw Material Transactions"
        indexes = [
            models.Index(fields=["kind", "when"]),
            models.Index(fields=["supplier_name"]),
            models.Index(fields=["dc_number"]),
        ]

    def __str__(self):
        rate = Decimal(self.rate_pkr or 0).quantize(Decimal("0.01"))
        return f"{self.get_kind_display()} · {self.qty_kg} kg @ {rate} PKR/kg"

    # ---- Company Stock bucket ---------------------------------------------
    @staticmethod
    def company_stock_customer():
        """
        Single sentinel 'customer' that represents company-owned stock.
        We key strictly by company_name to avoid dupes.
        """
        obj, _ = Customer.objects.get_or_create(
            company_name="__COMPANY_STOCK__",
            defaults={"country": "Pakistan"},  # keep it human-readable; no ISO coercion
        )
        return obj

    # ---- Validation & normalization ---------------------------------------
    def clean(self):
        from django.core.exceptions import ValidationError

        # Bags dominate explicit kg entry for PURCHASE
        if self.kind == self.Kind.PURCHASE:
            if (self.bags_count or 0) <= 0:
                raise ValidationError("Bags count must be > 0 for a purchase.")
            self.qty_kg = (Decimal(self.bags_count) * BAG_WEIGHT_KG).quantize(Decimal("0.001"))
        else:
            # for SALE/TRANSFER allow explicit kg (still accept bags as shortcut)
            if (self.bags_count or 0) > 0:
                self.qty_kg = (Decimal(self.bags_count) * BAG_WEIGHT_KG).quantize(Decimal("0.001"))

        q = dkg(self.bags_count or 0)
        if q <= Decimal("0"):
            raise ValidationError("Quantity must be > 0 (bags or kg).")

        # Endpoints per kind
        if self.kind == self.Kind.PURCHASE:
            if not self.supplier_name:
                raise ValidationError("Supplier name is required for a purchase.")
            self.from_customer = None
            self.to_customer = self.company_stock_customer()

        elif self.kind == self.Kind.SALE:
            if not self.to_customer:
                raise ValidationError("Target customer is required for a sale.")
            self.from_customer = self.company_stock_customer()

        elif self.kind == self.Kind.TRANSFER:
            if not (self.from_customer and self.to_customer):
                raise ValidationError("Both from_customer and to_customer are required for a transfer.")
            if self.from_customer_id == self.to_customer_id:
                raise ValidationError("From/To customers must be different.")
        else:
            raise ValidationError("Unknown transaction kind.")

        # Always compute amount for PURCHASE/SALE (keeps data consistent)
        if self.kind in (self.Kind.PURCHASE, self.Kind.SALE):
            rate_with_constant = Decimal(self.rate_pkr or 0) * Decimal("55")

            rate = Decimal(self.rate_pkr or 0)  # keep decimals!
            self.amount_pkr = (rate_with_constant * Decimal(self.bags_count or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            


    # ---- Apply to ledger (atomic) -----------------------------------------
    @transaction.atomic
    def apply(self, user=None):
        """
        Writes kg deltas to CustomerMaterialLedger and saves the txn atomically.
        PURCHASE: IN to company stock
        SALE:     OUT from company stock, IN to customer
        TRANSFER: OUT from from_customer, IN to to_customer
        """
        self.full_clean()

        if user and not self.created_by_id:
            self.created_by = user
        self.save()  # persist normalized fields

        kg = dkg(self.qty_kg)
        rate = int(self.rate_pkr)
        ledger_dt = _ledger_dt_for(self.when)  # ← use the chosen 'when' date (important!)

        if self.kind == self.Kind.PURCHASE:
            # PURCHASE (IN → company stock)
            CustomerMaterialLedger.objects.create(
                customer=self.to_customer, 
                order=None, 
                receipt=None, 
                date=ledger_dt,
                type=CustomerMaterialLedger.EntryType.IN, 
                delta_kg=kg,
                material_type=self.material_type,                                    # ← add
                memo=f"Purchase from {self.supplier_name} - {self.material_type} · {self.qty_kg} KG",
            )

        elif self.kind == self.Kind.SALE:
            # OUT from company stock; IN to customer
            comp = self.from_customer
            CustomerMaterialLedger.objects.create(
                customer=comp, order=None, receipt=None, date=ledger_dt,
                type=CustomerMaterialLedger.EntryType.OUT, delta_kg=-kg,
                material_type=self.material_type,                                    # ← add
                memo=f"Sale to {self.to_customer.company_name} - {self.material_type} ·  {self.qty_kg} KG",
            )
            CustomerMaterialLedger.objects.create(
            customer=self.to_customer, order=None, receipt=None, date=ledger_dt,
            type=CustomerMaterialLedger.EntryType.IN, delta_kg=kg,
            material_type=self.material_type,                                    # ← add
            memo=f"From Company Stock {self.qty_kg} KG - {self.material_type}",
            )

        else:  # TRANSFER
            CustomerMaterialLedger.objects.create(
                customer=self.from_customer, order=None, receipt=None, date=ledger_dt,
                type=CustomerMaterialLedger.EntryType.OUT, delta_kg=-kg,
                material_type=self.material_type,                                    # ← add
                memo=f"Transfer → {self.to_customer.company_name} - {self.material_type} - {self.qty_kg} KG",
        )
            CustomerMaterialLedger.objects.create(
                customer=self.to_customer, order=None, receipt=None, date=ledger_dt,
                type=CustomerMaterialLedger.EntryType.IN, delta_kg=kg,
                material_type=self.material_type,                                    # ← add
                memo=f"Transfer ← {self.from_customer.company_name} - {self.material_type} - {self.qty_kg} KG",
            )

        return self

    # ---- Supplier A/P helpers (for PURCHASE) ------------------------------
    @property
    def supplier_paid_pkr(self) -> int:
        """
        Sum of linked supplier payments (A/P). Safe for non-PURCHASE kinds (returns 0).
        """
        if self.kind != self.Kind.PURCHASE:
            return 0
        return int(self.linked_payments.aggregate(s=Sum("payment__amount_pkr"))["s"] or 0)

    @property
    def supplier_outstanding_pkr(self) -> int:
        """
        What you still owe the supplier for this purchase (never negative).
        """
        if self.kind != self.Kind.PURCHASE:
            return 0
        due = int(self.amount_pkr or 0) - int(self.supplier_paid_pkr or 0)
        return due if due > 0 else 0


# ------------------------ Supplier Payments (A/P) ---------------------------

class SupplierPayment(models.Model):
    """
    Outgoing payment you make to a raw-material supplier (A/P).
    You can link one payment to multiple purchases, or multiple payments to one purchase.
    """
    supplier_name = models.CharField(max_length=200)
    paid_on       = models.DateField(default=timezone.localdate)
    method        = models.CharField(max_length=20, choices=SUPPLIER_PAYMENT_METHODS, default="TRANSFER")
    bank          = models.CharField(choices=BANK_NAMES, blank=True, max_length=50)
    reference     = models.CharField(max_length=64, blank=True)  # cheque no., bank ref, etc.
    amount_pkr    = models.BigIntegerField(validators=[MinValueValidator(0)], help_text="Whole PKR (no decimals).")
    notes         = models.CharField(max_length=255, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    created_by    = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="supplier_payments_created"
    )

    class Meta:
        ordering = ["-paid_on", "-id"]
        verbose_name = "Supplier Payment"
        verbose_name_plural = "Supplier Payments"

    def __str__(self):
        return f"{self.paid_on} · PKR {int(self.amount_pkr or 0):,} · {self.supplier_name}"


class RawMaterialPurchasePayment(models.Model):
    """
    Link one or more SupplierPayments to a RawMaterialTxn (typically PURCHASE).
    """
    purchase = models.ForeignKey("RawMaterialTxn", on_delete=models.CASCADE, related_name="linked_payments")
    payment  = models.ForeignKey("SupplierPayment", on_delete=models.CASCADE, related_name="raw_material_links")
    note     = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [("purchase", "payment")]
        verbose_name = "Purchase ↔ Supplier Payment Link"
        verbose_name_plural = "Purchase ↔ Supplier Payment Links"

    def __str__(self):
        return f"Txn #{self.purchase_id} ↔ Pay #{self.payment_id}"