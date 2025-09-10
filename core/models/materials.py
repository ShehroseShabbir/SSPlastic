# core/models/materials.py  (your file shown above)

from django.utils import timezone
from decimal import Decimal
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.core.exceptions import ValidationError
from core.models.customers import Customer
from core.models.orders import Order
from core.utils_weight import dkg

# --- shared choices (keep simple; strings are what matter) ---
class MaterialKind(models.TextChoices):
    FILM = "FILM", "Film"
    TAPE = "TAPE", "Tape"

class MaterialReceipt(models.Model):
    """
    When a customer sends raw polythene bags as material.
    1 bag = 25 kg typically, but allow free entry too.
    """
    BAG_WEIGHT_KG = Decimal('25')  # default bag size

    customer   = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='material_receipts')
    date       = models.DateField(help_text="Date of Material Received")
    material_type = models.CharField(
        max_length=10,
        choices=MaterialKind.choices,
        blank=True,  # make optional so old rows don’t break; set default later if you want
    )
    bags_count = models.PositiveIntegerField(default=0)         # e.g. 4 bags
    extra_kg   = models.DecimalField(max_digits=10, decimal_places=3, default=0)  # non-bag kg if any
    notes      = models.CharField(max_length=255, blank=True)
    # Opening/correction flag (allows negatives)
    is_opening_adjustment = models.BooleanField(
        default=False,
        help_text="Tick for one-time opening/correction. Allows negative kg and syncs to ledger as adjustment."
    )

    @property
    def total_kg(self):
        return dkg(self.bags_count) * self.BAG_WEIGHT_KG + dkg(self.extra_kg)

    def clean(self):
        super().clean()
        total = self.total_kg
        if self.is_opening_adjustment:
            if total == 0:
                raise ValidationError("Opening adjustment total cannot be 0 kg.")
        else:
            if total <= 0:
                raise ValidationError("Material Receipt must be > 0 kg. For negatives, tick 'opening adjustment'.")

    def __str__(self):
        tag = " (ADJ)" if self.is_opening_adjustment else ""
        mt = f" · {self.get_material_type_display()}" if self.material_type else ""
        return f"{self.customer.company_name} · {self.date}{mt}{tag} · {self.total_kg} kg"

    class Meta:
        verbose_name = "Material Receipt"
        verbose_name_plural = "Material Receipts"


class CustomerMaterialLedger(models.Model):
    class EntryType(models.TextChoices):
        IN  = "IN", "In"
        OUT = "OUT", "Out"

    # If you prefer, you can re-use MaterialKind above here too:
    material_type = models.CharField(
        max_length=10,
        choices=MaterialKind.choices,
        blank=True,
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="material_ledger")
    order    = models.ForeignKey(
        Order, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="material_ledger_entries"
    )
    receipt  = models.ForeignKey(
        MaterialReceipt, null=True, blank=True, on_delete=models.CASCADE
    )

    date     = models.DateTimeField(default=timezone.now)  # <- replace auto_now_add=True
    type     = models.CharField(max_length=3, choices=EntryType.choices)
    delta_kg = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0.000"))
    memo     = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["order", "type"],
                name="uniq_out_per_order",
                condition=Q(type="OUT"),
            )
        ]
        verbose_name = "Ledger"
        verbose_name_plural = "Ledgers"

    def __str__(self):
        return f"{self.customer.company_name} {self.type} {self.delta_kg} kg"
