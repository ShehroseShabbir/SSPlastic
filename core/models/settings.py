from decimal import Decimal
from django.db import models

class SiteSettings(models.Model):
    # Branding / company info
    company_name = models.CharField(max_length=255, blank=True, default="")
    company_address = models.TextField(blank=True, default="")  # multi-line
    bank_details = models.TextField(blank=True, default="")     # multi-line
    notes = models.TextField(blank=True, default="", help_text="Please put any notes you want to include in bills in footer")     # multi-line
    logo = models.ImageField(upload_to="branding/", null=True, blank=True)

    # Tax
    tax_label = models.CharField(max_length=20, default="GST", blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))  # e.g., 17.00

    # Optional SMTP overrides (leave blank to use project settings.py)
    email_backend = models.CharField(max_length=200, blank=True, default="", help_text="e.g. django.core.mail.backends.smtp.EmailBackend")
    email_host = models.CharField(max_length=200, blank=True, default="")
    email_port = models.PositiveIntegerField(null=True, blank=True)
    email_use_tls = models.BooleanField(default=True)
    email_host_user = models.CharField(max_length=200, blank=True, default="")
    email_host_password = models.CharField(max_length=200, blank=True, default="", help_text="Consider env vars instead of storing here.")

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return self.company_name or "Site Settings"

    # Helpers to turn TextField → list of lines
     # Helpers used by the invoice code
    @property
    def company_address_list(self):
        return [ln.strip() for ln in (self.company_address or "").splitlines() if ln.strip()]
    
    @property
    def notes_list(self):
        return [ln.strip() for ln in (self.notes or "").splitlines() if ln.strip()]

    @property
    def bank_details_list(self):
        return [ln.strip() for ln in (self.bank_details or "").splitlines() if ln.strip()]
