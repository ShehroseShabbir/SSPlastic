# core/apps.py
from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig as DjangoAdminConfig

class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Operations"   # ← the sidebar section title for this app

class SSPAdminConfig(DjangoAdminConfig):
    # Use our custom AdminSite class
    default_site = "core.admin_site.SSPAdminSite"
