from django.db import models
from django.utils import timezone
# -------------------------
# EMPLOYEES / LABOUR
# -------------------------
class Employee(models.Model):
    """
    Store labour/staff info + photo and contract/documents.
    """
    name = models.CharField(max_length=150)
    cnic = models.CharField(max_length=20, blank=True)  # Pakistan CNIC format e.g., 42101-1234567-1
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    salary = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # monthly salary
    join_date = models.DateField(null=True, blank=True)
    hire_date = models.DateField(null=True, blank=True)  # alias if needed separately
    contract_end_date = models.DateField(null=True, blank=True)

    profile_picture = models.ImageField(upload_to='employees/photos/', null=True, blank=True)
    contract_file = models.FileField(upload_to='employees/contracts/', null=True, blank=True)

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Attendance(models.Model):
    """
    Daily attendance for employees.
    """
    STATUS_CHOICES = [
        ('P', 'Present'),
        ('A', 'Absent'),
        ('L', 'Leave'),
        ('H', 'Half Day'),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default='P')
    hours_worked = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # optional
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ('employee', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"{self.employee.name} - {self.date} - {self.get_status_display()}"

