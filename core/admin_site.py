# core/admin_site.py
from django.contrib.admin import AdminSite

class SSPAdminSite(AdminSite):
    site_header = "SS Plastic Admin"
    site_title  = "SS Plastic"
    index_title = "Back Office"

    def get_app_list(self, request, app_label=None):
        """
        Django 5.x passes (request, app_label) on app index pages.
        Keep compatibility with older versions too.
        """
        # Call the parent with/without app_label depending on support
        try:
            app_list = super().get_app_list(request, app_label)  # Django ≥5
        except TypeError:
            app_list = super().get_app_list(request)             # Django <5

        # Only do our custom grouping on the main dashboard (no app_label).
        if app_label:
            return app_list

        # --- Your grouping logic (example) ---
        core = next((a for a in app_list if a["app_label"] == "core"), None)
        if not core:
            return app_list

        core_models = core["models"]

        def pick(name):
            return next((m for m in core_models if m["name"] == name), None)

        buckets = [
            ("Accounting",    [pick("Payments"), pick("Expenses"), pick("Payment allocations"), pick("Material receipts"), pick("Customer material ledgers")]),
            ("Production",    [pick("Customers"), pick("Orders")]),
            ("HR",            [pick("Employees"), pick("Attendances"), pick("Salary payments")]),
            ("Configuration", [pick("Site Settings"), pick("Expense Categories")]),
        ]

        grouped = []
        for title, models in buckets:
            models = [m for m in models if m]
            if models:
                grouped.append({
                    "name": title, "app_label": "core", "app_url": "",
                    "models": models,
                })

        # If you have other apps, append them too:
        for app in app_list:
            if app["app_label"] != "core":
                grouped.append(app)

        return grouped
