from django import forms
from decimal import Decimal

from core.models.common import money_int_pk
from core.utils_weight import dkg
from ..models import Customer, RawMaterialTxn


class PurchaseForm(forms.Form):
    supplier_name = forms.CharField(max_length=200, label="Supplier")
    qty_kg = forms.DecimalField(label="Quantity (kg)", min_value=Decimal("0.001"), decimal_places=3)
    rate_pkr = forms.IntegerField(label="Rate (PKR/kg)", min_value=0)
    memo = forms.CharField(required=False, max_length=255)

    def save(self, user=None):
        txn = RawMaterialTxn(
            kind=RawMaterialTxn.Kind.PURCHASE,
            supplier_name=self.cleaned_data["supplier_name"],
            qty_kg=dkg(self.cleaned_data["qty_kg"]),
            rate_pkr=int(self.cleaned_data["rate_pkr"]),
            memo=self.cleaned_data.get("memo", ""),
            created_by=user,
        )
        txn.amount_pkr = money_int_pk(D(txn.rate_pkr) * txn.qty_kg)
        return txn.apply(user=user)

class SellForm(forms.Form):
    to_customer = forms.ModelChoiceField(queryset=Customer.objects.all())
    qty_kg = forms.DecimalField(label="Quantity (kg)", min_value=Decimal("0.001"), decimal_places=3)
    rate_pkr = forms.IntegerField(label="Sale Rate (PKR/kg)", min_value=0)
    memo = forms.CharField(required=False, max_length=255)

    def save(self, user=None):
        txn = RawMaterialTxn(
            kind=RawMaterialTxn.Kind.SALE,
            to_customer=self.cleaned_data["to_customer"],
            qty_kg=dkg(self.cleaned_data["qty_kg"]),
            rate_pkr=int(self.cleaned_data["rate_pkr"]),
            memo=self.cleaned_data.get("memo", ""),
            created_by=user,
        )
        txn.amount_pkr = money_int_pk(D(txn.rate_pkr) * txn.qty_kg)
        return txn.apply(user=user)

class TransferForm(forms.Form):
    from_customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="From")
    to_customer   = forms.ModelChoiceField(queryset=Customer.objects.all(), label="To")
    qty_kg = forms.DecimalField(label="Quantity (kg)", min_value=Decimal("0.001"), decimal_places=3)
    memo = forms.CharField(required=False, max_length=255)

    def clean(self):
        c = super().clean()
        if c.get("from_customer") and c.get("to_customer") and c["from_customer"] == c["to_customer"]:
            self.add_error("to_customer", "From/To cannot be the same.")
        return c

    def save(self, user=None):
        txn = RawMaterialTxn(
            kind=RawMaterialTxn.Kind.TRANSFER,
            from_customer=self.cleaned_data["from_customer"],
            to_customer=self.cleaned_data["to_customer"],
            qty_kg=dkg(self.cleaned_data["qty_kg"]),
            rate_pkr=0,
            memo=self.cleaned_data.get("memo", ""),
            created_by=user,
        )
        txn.amount_pkr = 0
        return txn.apply(user=user)