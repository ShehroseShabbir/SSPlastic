from django.db import models
from django.utils import timezone

from core.models.hr import Employee


class SalaryPayment(models.Model):
    """
    Records monthly salary payments to employees.
    """
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='salary_payments')
    period_month = models.PositiveSmallIntegerField()  # 1-12
    period_year = models.PositiveIntegerField()
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.BigIntegerField()
    payment_date = models.DateField(default=timezone.now)
    method = models.CharField(max_length=40, default='Cash', blank=True)  # Cash/Bank Transfer/Cheque
    notes = models.TextField(blank=True)
    slip = models.FileField(upload_to='employees/salary_slips/', null=True, blank=True)

    class Meta:
        unique_together = ('employee', 'period_month', 'period_year')
        ordering = ['-period_year', '-period_month']

    @property
    def outstanding(self) -> int:
        return int((self.gross_amount_rupees or 0) - (self.paid_amount_rupees or 0))

    def __str__(self):
        return f"{self.employee.name} - {self.period_month}/{self.period_year}"
    
    # -------------------------
# EXPENSES
# -------------------------
class ExpenseCategory(models.Model):
    """
    Categories like Electricity (KE), Gas, Factory Expense, Labour Cost etc.
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Expense Categories"

    def __str__(self):
        return self.name


class Expense(models.Model):
    """
    Individual expense entries, optionally monthly.
    """
    PERIOD_CHOICES = [
        ('ONCE', 'One-time'),
        ('MONTHLY', 'Monthly'),
        ('YEARLY', 'Yearly'),
    ]
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name='expenses')
    title = models.CharField(max_length=200)
    amount = models.BigIntegerField()
    expense_date = models.DateField(default=timezone.now)
    period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='ONCE')
    notes = models.TextField(blank=True)
    attachment = models.FileField(upload_to='expenses/', blank=True, null=True)  # e.g., bill scan/PDF

    def __str__(self):
        return f"{self.title} - {self.amount} ({self.category.name})"