# core/models/raw_material.py
from decimal import Decimal, ROUND_HALF_UP
from django.db import models, transaction
from django.utils import timezone
from django.core.validators import MinValueValidator

from core.models.customers import Customer
from core.models.materials import CustomerMaterialLedger  # adjust import path if different
from core.utils_weight import dkg  # returns Decimal with 3dp

BAG_WEIGHT_KG = Decimal("25.000")
DEFAULT_COUNTRY_CODE = getattr(settings, "DEFAULT_COUNTRY_CODE", "PK")


class RawMaterialTxn(models.Model):
    """
    Auditable record of raw-material movements & commercial terms.
    Money in whole PKR; quantity in kg (Decimal 3dp).
    All kg effects are written to CustomerMaterialLedger via .apply() atomically.
    """

    # What the transaction does
    class Kind(models.TextChoices):
        PURCHASE = "PURCHASE", "Purchase (into Company Stock)"
        SALE     = "SALE",     "Sell from Company Stock to Customer"
        TRANSFER = "TRANSFER", "Customer → Customer transfer"

    # Material category (for your reporting)
    class MaterialKind(models.TextChoices):
        FILM = "FILM", "Film"
        TAPE = "TAPE", "Tape"

    # Core attributes
    kind        = models.CharField(max_length=12, choices=Kind.choices,
                                   default=Kind.PURCHASE)
    when        = models.DateField(default=timezone.localdate)

    # Parties
    supplier_name = models.CharField(max_length=200, blank=True)  # for PURCHASE
    from_customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT,
        related_name="rm_out_txns"
    )
    to_customer   = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.PROTECT,
        related_name="rm_in_txns"
    )

    # Quantities & money
    qty_kg     = models.DecimalField(max_digits=12, decimal_places=3,
                                     validators=[MinValueValidator(Decimal("0.001"))])
    rate_pkr   = models.BigIntegerField(default=0,
                                        validators=[MinValueValidator(0)],
                                        help_text="Rate per kg (PKR, whole rupees).")
    amount_pkr = models.BigIntegerField(default=0,
                                        validators=[MinValueValidator(0)],
                                        help_text="Total amount in PKR (ints).")

    # Extra capture for your workflow
    material_type = models.CharField(max_length=10,
                                     choices=MaterialKind.choices,
                                     default=MaterialKind.FILM)
    bags_count    = models.PositiveIntegerField(default=0,
                                                help_text="Each bag = 25 kg.")
    dc_number     = models.CharField(max_length=50, blank=True,
                                     help_text="Delivery challan / reference.")

    # Notes & audit
    memo       = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="rm_created"
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
        return f"{self.get_kind_display()} · {self.qty_kg} kg @ {self.rate_pkr} PKR"

    # ---- Company Stock bucket ---------------------------------------------
    @staticmethod
    def company_stock_customer():
        obj, _ = Customer.objects.get_or_create(
            company_name="__COMPANY_STOCK__",
            defaults={"country": DEFAULT_COUNTRY_CODE},  # <-- was "Pakistan"
        )
        # if an old bad row exists, normalize it
        if len(str(obj.country)) > 2:
            obj.country = "PK"
            obj.save(update_fields=["country"])
        return obj

    # ---- Validation & normalization ---------------------------------------
    def clean(self):
        from django.core.exceptions import ValidationError

        # 1) Derive qty_kg from bags if provided (bags dominate explicit kg)
        if (self.bags_count or 0) > 0:
            self.qty_kg = (Decimal(self.bags_count) * BAG_WEIGHT_KG).quantize(Decimal("0.001"))

        q = dkg(self.qty_kg or 0)
        if q <= Decimal("0"):
            raise ValidationError("Quantity must be > 0 (bags or kg).")

        # 2) Endpoints logic per kind
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

        # 3) Compute amount if not set (only for PURCHASE/SALE)
        if (not self.amount_pkr) and (self.kind in (self.Kind.PURCHASE, self.Kind.SALE)):
            self.amount_pkr = int((Decimal(int(self.rate_pkr)) * q).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

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

        self.save()  # saves normalized fields & amounts

        kg = dkg(self.qty_kg)
        rate = int(self.rate_pkr)
        now_dt = timezone.now()

        if self.kind == self.Kind.PURCHASE:
            # IN to company stock
            CustomerMaterialLedger.objects.create(
                customer=self.to_customer, order=None, receipt=None, date=now_dt,
                type=CustomerMaterialLedger.EntryType.IN, delta_kg=kg,
                memo=f"Purchase from {self.supplier_name} @ {rate} PKR/kg · Txn {self.pk}",
            )

        elif self.kind == self.Kind.SALE:
            # OUT company stock; IN to customer
            comp = self.from_customer
            CustomerMaterialLedger.objects.create(
                customer=comp, order=None, receipt=None, date=now_dt,
                type=CustomerMaterialLedger.EntryType.OUT, delta_kg=-kg,
                memo=f"Sale to {self.to_customer.company_name} @ {rate} PKR/kg · Txn {self.pk}",
            )
            CustomerMaterialLedger.objects.create(
                customer=self.to_customer, order=None, receipt=None, date=now_dt,
                type=CustomerMaterialLedger.EntryType.IN, delta_kg=kg,
                memo=f"From Company Stock @ {rate} PKR/kg · Txn {self.pk}",
            )

        else:  # TRANSFER
            CustomerMaterialLedger.objects.create(
                customer=self.from_customer, order=None, receipt=None, date=now_dt,
                type=CustomerMaterialLedger.EntryType.OUT, delta_kg=-kg,
                memo=f"Transfer → {self.to_customer.company_name} · Txn {self.pk}",
            )
            CustomerMaterialLedger.objects.create(
                customer=self.to_customer, order=None, receipt=None, date=now_dt,
                type=CustomerMaterialLedger.EntryType.IN, delta_kg=kg,
                memo=f"Transfer ← {self.from_customer.company_name} · Txn {self.pk}",
            )

        return self
    

class RawMaterialPurchasePayment(models.Model):
    """
    Link one or more Payments to a RawMaterialTxn (typically PURCHASE).
    """
    purchase = models.ForeignKey("RawMaterialTxn", on_delete=models.CASCADE, related_name="linked_payments")
    payment  = models.ForeignKey("Payment", on_delete=models.CASCADE, related_name="raw_material_links")
    note     = models.CharField(max_length=120, blank=True)

    class Meta:
        unique_together = [("purchase","payment")]