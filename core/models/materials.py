from decimal import Decimal
from django.db import models
from django.db.models import Q, UniqueConstraint

from core.utils_weight import dkg

# MaterialReceipt
class MaterialReceipt(models.Model):
    """
    When a customer sends raw polythene bags as material.
    1 bag = 25 kg typically, but allow free entry too.
    """
    BAG_WEIGHT_KG = Decimal('25')  # default bag size

    customer = models.ForeignKey('Customer', on_delete=models.CASCADE, related_name='material_receipts')
    date = models.DateField(help_text="Date of Material Received")
    bags_count = models.PositiveIntegerField(default=0)         # e.g. 4 bags
    extra_kg = models.DecimalField(max_digits=10, decimal_places=3, default=0)  # non-bag kg if any
    notes = models.CharField(max_length=255, blank=True)

    @property
    def total_kg(self):
        return dkg(self.bags_count) * self.BAG_WEIGHT_KG + dkg(self.extra_kg)

    def __str__(self):
        return f"{self.customer.company_name} · {self.date} · {self.total_kg} kg"
    class Meta:
        verbose_name = "Material Receipt"
        verbose_name_plural = "Material Receipts"
    
class CustomerMaterialLedger(models.Model):
    class EntryType(models.TextChoices):
        IN = "IN", "In"
        OUT = "OUT", "Out"

    customer = models.ForeignKey(
        "Customer", on_delete=models.CASCADE, related_name="material_ledger"
    )
    # keep ONLY ONE order FK
    order = models.ForeignKey(
        "Order", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="material_ledger_entries"
    )
    receipt = models.ForeignKey(
        "MaterialReceipt", null=True, blank=True, on_delete=models.SET_NULL
    )
    

    date = models.DateTimeField(auto_now_add=True)
    type = models.CharField(max_length=3, choices=EntryType.choices)
    delta_kg = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0.000"))
    memo = models.CharField(max_length=255, blank=True)

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