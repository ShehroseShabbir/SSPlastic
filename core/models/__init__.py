# core/models/__init__.py

# Re-export models so external imports don't break
from .customers import Customer
from .orders import Order, OrderItem, OrderRoll
from .materials import MaterialReceipt, CustomerMaterialLedger
from .accounting import ExpenseCategory, Expense, SalaryPayment
from .hr import Employee, Attendance
from .settings import SiteSettings
from .raw_material import RawMaterialTxn  # <-- new

__all__ = [
    "Customer",
    "Order", "OrderItem", "OrderRoll",
    "MaterialReceipt", "CustomerMaterialLedger",
    "ExpenseCategory", "Expense", "SalaryPayment",
    "Employee", "Attendance",
    "SiteSettings", "RawMaterialTxn"
]
