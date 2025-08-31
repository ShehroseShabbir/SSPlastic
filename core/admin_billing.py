# core/admin_billing.py
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from core.models.models_billing import MonthlyStatement, StatementLine

# ----- Inline (read-only) lines on a statement -----
class StatementLineInline(admin.TabularInline):
    model = StatementLine
    extra = 0
    fields = ("date", "line_type", "description", "order", "payment", "qty_kg", "rate", "amount_pkr")
    readonly_fields = fields
    can_delete = False
    def has_add_permission(self, request, obj=None):  # lines are created by signals/services
        return False

@admin.register(MonthlyStatement)
class MonthlyStatementAdmin(admin.ModelAdmin):
    list_display = ("customer", "period", "opening_pkr", "charges_pkr", "credits_pkr", "closing_pkr", "frozen")
    list_filter  = ("year", "month", "frozen")
    search_fields = ("customer__company_name",)
    inlines = [StatementLineInline]
    readonly_fields = ("opening_pkr", "charges_pkr", "credits_pkr", "closing_pkr", "generated_at")

    def period(self, obj):
        return f"{obj.year}-{obj.month:02d}"

    @admin.action(description="Freeze selected statements")
    def freeze_statements(self, request, queryset):
        queryset.update(frozen=True)
    @admin.action(description="Unfreeze selected statements")
    def unfreeze_statements(self, request, queryset):
        queryset.update(frozen=False)
    actions = ["freeze_statements", "unfreeze_statements"]
