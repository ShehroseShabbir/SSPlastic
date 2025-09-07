# core/apps.py
from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig as DjangoAdminConfig 
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.apps import apps

class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Operations"   # ← the sidebar section title for this app
    def ready(self):
        import core.services.signals_billing  # noqa
        import core.services.material_sync  # noqa
        from core.services.ar_simple import refresh_customer_pending, auto_allocate_payment, allocate_unapplied_for_customer, FINAL_STATES


        Payment = apps.get_model("core","Payment")
        Order   = apps.get_model("core","Order")

        @receiver(post_save, sender=Payment)
        def _pay_saved(sender, instance, **kwargs):
            auto_allocate_payment(instance.pk, reset_existing=True)
            refresh_customer_pending(instance.customer_id)

        @receiver(post_delete, sender=Payment)
        def _pay_deleted(sender, instance, **kwargs):
            refresh_customer_pending(instance.customer_id)

        @receiver(post_save, sender=Order)
        def _order_saved(sender, instance, **kwargs):
            # if the order is final/billable, ensure unapplied cash is used; then refresh pending
            if instance.status in FINAL_STATES:
                allocate_unapplied_for_customer(instance.customer_id)
            refresh_customer_pending(instance.customer_id)

        @receiver(post_delete, sender=Order)
        def _order_deleted(sender, instance, **kwargs):
            refresh_customer_pending(instance.customer_id)

class SSPAdminConfig(DjangoAdminConfig):
    # Use our custom AdminSite class
    default_site = "core.admin_site.SSPAdminSite"
    
