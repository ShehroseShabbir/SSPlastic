# core/admin.py
from django.core.cache import cache
from django.contrib import admin

from core.models.settings import SiteSettings

def _splitlines(val):
    if not val:
        return []
    return [ln.strip() for ln in str(val).splitlines() if ln.strip()]

def get_site_settings():
    """
    Return the first SiteSettings with convenience attributes:
    - company_address_list: list[str]
    - bank_details_list: list[str]
    Ensures your utils.py can consume them directly.
    """
    ss = SiteSettings.objects.first()
    if not ss:
        return None

    # Attach computed lists if the model doesn't already define them
    if not hasattr(ss, "company_address"):
        ss.company_address_list = _splitlines(getattr(ss, "company_address", ""))
    if not hasattr(ss, "bank_details"):
        ss.bank_details_list = _splitlines(getattr(ss, "bank_details", ""))

    # Normalize tax fields:
    # If ss.tax_rate is stored as "17.00" (percent), leave it as-is here.
    # utils.py already divides by 100 when using SiteSettings.
    return ss

@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("company_name", "tax_label", "tax_rate")
    fieldsets = (
        ("Branding", {
            "fields": ("company_name", "logo", "company_address")
        }),
        ("Tax", {
            "fields": ("tax_label", "tax_rate")
        }),
        ("Banking (PDF footer)", {
            "fields": ("bank_details",)
        }),
        ("Email (optional overrides)", {
            "description": "Leave blank to use project settings.py EMAIL_* values.",
            "fields": ("email_backend", "email_host", "email_port", "email_use_tls",
                       "email_host_user", "email_host_password")
        }),
    )

    def has_add_permission(self, request):
        # Allow add only if no instance exists (singleton)
        return not SiteSettings.objects.exists()

    def changelist_view(self, request, extra_context=None):
        # Redirect list → edit page for the single instance
        obj = SiteSettings.objects.first()
        if obj:
            from django.shortcuts import redirect
            return redirect(f"/admin/core/sitesettings/{obj.pk}/change/")
        return super().changelist_view(request, extra_context)
