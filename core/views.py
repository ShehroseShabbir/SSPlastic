from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from collections import defaultdict
from decimal import Decimal
from .models import (
    Customer, Order, OrderItem, OrderRoll, Expense, ExpenseCategory,
    Employee, Attendance, CustomerMaterialLedger
)
from django.shortcuts import render
from .models_ar import PaymentAllocation
from django.db.models import (
    Sum, F, Value, ExpressionWrapper, DecimalField, OuterRef, Subquery, Count, Q
)
from django.db.models.functions import Coalesce, TruncDate, TruncMonth, ExtractYear
from django.utils import timezone
import datetime, csv,calendar

# common field types
MONEY = DecimalField(max_digits=18, decimal_places=2)
KG    = DecimalField(max_digits=12, decimal_places=3)


# -----------------------------
# HELPERS
# -----------------------------
def _orders_with_totals(qs):
    """Annotate orders with calculated totals and payments."""
    item_amount_expr = ExpressionWrapper(
        F('roll_weight') * F('quantity') * F('price_per_kg'),
        output_field=MONEY,
    )

    items_total_sq = (
        OrderItem.objects
        .filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum(item_amount_expr), Value(0, output_field=MONEY)))
        .values('total')[:1]
    )

    allocations_total_sq = (
        PaymentAllocation.objects
        .filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum('amount'), Value(0, output_field=MONEY)))
        .values('total')[:1]
    )

    return qs.annotate(
        total_amount_calc=Coalesce(Subquery(items_total_sq, output_field=MONEY), Value(0, output_field=MONEY)),
        total_paid_calc=Coalesce(Subquery(allocations_total_sq, output_field=MONEY), Value(0, output_field=MONEY)),
        outstanding=ExpressionWrapper(
            F("total_amount_calc") - F("total_paid_calc"),
            output_field=MONEY
        )
    )


# -----------------------------
# VIEWS
# -----------------------------
def customer_balances(request):
    # Material balances
    material_by_customer = dict(
        CustomerMaterialLedger.objects
        .values('customer_id')
        .annotate(balance=Coalesce(Sum('delta_kg'), Value(0, output_field=KG)))
        .values_list('customer_id', 'balance')
    )

    # Outstanding balances
    outstanding_by_customer = defaultdict(Decimal)
    orders = _orders_with_totals(Order.objects.select_related('customer').only('id', 'customer_id'))
    for o in orders:
        outstanding_by_customer[o.customer_id] += (o.outstanding or Decimal('0'))

    # Build rows
    rows = []
    customers = Customer.objects.all().order_by('company_name')
    for c in customers:
        mat = Decimal(material_by_customer.get(c.id, 0) or 0)
        out = Decimal(outstanding_by_customer.get(c.id, 0) or 0)
        rows.append({
            'id': c.id,
            'company_name': c.company_name,
            'contact_name': c.contact_name,
            'phone': c.phone,
            'material_balance_kg': mat,
            'outstanding_amount': out,
        })

    # CSV export
    if request.GET.get('format') == 'csv':
        resp = HttpResponse(content_type='text/csv')
        resp['Content-Disposition'] = 'attachment; filename="customer_balances.csv"'
        writer = csv.writer(resp)
        writer.writerow(['Company', 'Contact', 'Phone', 'Material Balance (kg)', 'Outstanding Amount'])
        for r in rows:
            writer.writerow([r['company_name'], r['contact_name'], r['phone'], f"{r['material_balance_kg']:.3f}", f"{r['outstanding_amount']:.2f}"])
        return resp

    return render(request, 'core/customer_balances.html', {'rows': rows})

@login_required(login_url='/admin/login/')
def dashboard(request):
    today = timezone.now().date()

    # ---------- SALES subtotal expression (target_total_kg * price_per_kg) ----------
    sales_subtotal_expr = ExpressionWrapper(
        F("target_total_kg") * F("price_per_kg"),
        output_field=MONEY,
    )

    # ---------- STATS (CARDS) ----------
    # total production: prefer rolls, fallback to items if you don't have rolls yet
    if OrderRoll.objects.exists():
        total_production_val = (
            OrderRoll.objects.aggregate(
                s=Coalesce(Sum("weight_kg", output_field=KG),
                           Value(Decimal("0.000"), output_field=KG))
            )["s"] or Decimal("0.000")
        )
    else:
        total_production_val = (
            OrderItem.objects.aggregate(
                s=Coalesce(Sum(F("roll_weight") * F("quantity"), output_field=KG),
                           Value(Decimal("0.000"), output_field=KG))
            )["s"] or Decimal("0.000")
        )

    stats = {
        "total_orders": Order.objects.count(),
        "open_orders": Order.objects.filter(~Q(status__in=["DELIVERED", "CLOSED"])).count(),
        "closed_orders": Order.objects.filter(status__in=["DELIVERED", "CLOSED"]).count(),
        "overdue_orders": (
            Order.objects.exclude(status__in=["DRAFT", "DELIVERED", "CLOSED"])
            .filter(delivery_date__lt=today)
            .count()
        ),
        "pending_payments": Order.objects.filter(~Q(status__in=["CLOSED", "DELIVERED"])).count(),
        "total_material": (
            CustomerMaterialLedger.objects.aggregate(
                s=Coalesce(Sum("delta_kg", output_field=KG),
                           Value(Decimal("0.000"), output_field=KG))
            ).get("s") or Decimal("0.000")
        ),
        "total_production": total_production_val,
    }

    # ---------- SALES datasets (Daily / Monthly / Yearly) ----------
    start_30 = today - datetime.timedelta(days=29)

    sales_daily_qs = (
        Order.objects.filter(order_date__range=[start_30, today])
        .annotate(subtotal=sales_subtotal_expr)
        .values("order_date")
        .annotate(total=Coalesce(Sum("subtotal", output_field=MONEY), Value(0, output_field=MONEY)))
        .order_by("order_date")
    )
    sales_daily_labels = [row["order_date"].strftime("%d %b") for row in sales_daily_qs]
    sales_daily_values = [float(row["total"] or 0) for row in sales_daily_qs]

    sales_monthly_qs = (
        Order.objects.filter(order_date__year=today.year)
        .annotate(subtotal=sales_subtotal_expr)
        .values("order_date__month")
        .annotate(total=Coalesce(Sum("subtotal", output_field=MONEY), Value(0, output_field=MONEY)))
        .order_by("order_date__month")
    )
    sales_monthly_labels = [calendar.month_abbr[row["order_date__month"]] for row in sales_monthly_qs]
    sales_monthly_values = [float(row["total"] or 0) for row in sales_monthly_qs]

    sales_yearly_qs = (
        Order.objects
        .annotate(subtotal=sales_subtotal_expr)
        .values("order_date__year")
        .annotate(total=Coalesce(Sum("subtotal", output_field=MONEY), Value(0, output_field=MONEY)))
        .order_by("order_date__year")
    )
    sales_yearly_labels = [str(row["order_date__year"]) for row in sales_yearly_qs]
    sales_yearly_values = [float(row["total"] or 0) for row in sales_yearly_qs]

    # ---------- PRODUCTION datasets (Daily / Monthly / Yearly) ----------
    if OrderRoll.objects.exists():
        # Use roll timestamps
        prod_daily_qs = (
            OrderRoll.objects
            .filter(created_at__date__range=[start_30, today])
            .annotate(d=TruncDate("created_at"))
            .values("d")
            .annotate(kg=Coalesce(Sum("weight_kg", output_field=KG), Value(0, output_field=KG)))
            .order_by("d")
        )
        production_daily_labels = [row["d"].strftime("%d %b") for row in prod_daily_qs]
        production_daily_values = [float(row["kg"] or 0) for row in prod_daily_qs]

        prod_monthly_qs = (
            OrderRoll.objects
            .filter(created_at__year=today.year)
            .annotate(m=TruncMonth("created_at"))
            .values("m")
            .annotate(kg=Coalesce(Sum("weight_kg", output_field=KG), Value(0, output_field=KG)))
            .order_by("m")
        )
        production_monthly_labels = [row["m"].strftime("%b") for row in prod_monthly_qs]
        production_monthly_values = [float(row["kg"] or 0) for row in prod_monthly_qs]

        prod_yearly_qs = (
            OrderRoll.objects
            .annotate(y=ExtractYear("created_at"))
            .values("y")
            .annotate(kg=Coalesce(Sum("weight_kg", output_field=KG), Value(0, output_field=KG)))
            .order_by("y")
        )
        production_yearly_labels = [str(row["y"]) for row in prod_yearly_qs]
        production_yearly_values = [float(row["kg"] or 0) for row in prod_yearly_qs]

    else:
        # Fallback to order items (if you haven’t started recording rolls yet)
        prod_daily_qs = (
            OrderItem.objects
            .filter(order__order_date__range=[start_30, today])
            .values("order__order_date")
            .annotate(kg=Coalesce(Sum(F("roll_weight") * F("quantity"), output_field=KG),
                                  Value(0, output_field=KG)))
            .order_by("order__order_date")
        )
        production_daily_labels = [row["order__order_date"].strftime("%d %b") for row in prod_daily_qs]
        production_daily_values = [float(row["kg"] or 0) for row in prod_daily_qs]

        prod_monthly_qs = (
            OrderItem.objects
            .filter(order__order_date__year=today.year)
            .values("order__order_date__month")
            .annotate(kg=Coalesce(Sum(F("roll_weight") * F("quantity"), output_field=KG),
                                  Value(0, output_field=KG)))
            .order_by("order__order_date__month")
        )
        production_monthly_labels = [calendar.month_abbr[row["order__order_date__month"]] for row in prod_monthly_qs]
        production_monthly_values = [float(row["kg"] or 0) for row in prod_monthly_qs]

        prod_yearly_qs = (
            OrderItem.objects
            .values("order__order_date__year")
            .annotate(kg=Coalesce(Sum(F("roll_weight") * F("quantity"), output_field=KG),
                                  Value(0, output_field=KG)))
            .order_by("order__order_date__year")
        )
        production_yearly_labels = [str(row["order__order_date__year"]) for row in prod_yearly_qs]
        production_yearly_values = [float(row["kg"] or 0) for row in prod_yearly_qs]

    # ---------- TOP OUTSTANDING (same as you had) ----------
    billed_per_customer = {
        (row["customer_id"], row["customer__company_name"]): row["billed"]
        for row in (
            Order.objects
            .annotate(subtotal=sales_subtotal_expr)
            .values("customer_id", "customer__company_name")
            .annotate(billed=Coalesce(Sum("subtotal", output_field=MONEY), Value(0, output_field=MONEY)))
        )
    }
    paid_per_customer = {
        (row["order__customer_id"], row["order__customer__company_name"]): row["paid"]
        for row in (
            PaymentAllocation.objects
            .values("order__customer_id", "order__customer__company_name")
            .annotate(paid=Coalesce(Sum("amount", output_field=MONEY), Value(0, output_field=MONEY)))
        )
    }
    top_outstanding = []
    all_keys = set(billed_per_customer.keys()) | set(paid_per_customer.keys())
    for key in all_keys:
        billed = Decimal(billed_per_customer.get(key, 0) or 0)
        paid = Decimal(paid_per_customer.get(key, 0) or 0)
        pending = billed - paid
        if pending > 0:
            top_outstanding.append({"customer_name": key[1], "amount": pending})
    top_outstanding.sort(key=lambda x: x["amount"], reverse=True)
    top_outstanding = top_outstanding[:8]

    # ---------- LOW STOCK ----------
    low_stock_qs = (
        CustomerMaterialLedger.objects
        .values("customer__id", "customer__company_name")
        .annotate(balance=Coalesce(Sum("delta_kg", output_field=KG), Value(0, output_field=KG)))
        .order_by("balance")
    )
    low_stock = [
        {"customer_name": row["customer__company_name"], "balance": row["balance"] or Decimal("0.000")}
        for row in low_stock_qs[:8]
    ]

    context = {
        "stats": stats,

        # sales datasets
        "sales_daily_labels": sales_daily_labels,
        "sales_daily_values": sales_daily_values,
        "sales_monthly_labels": sales_monthly_labels,
        "sales_monthly_values": sales_monthly_values,
        "sales_yearly_labels": sales_yearly_labels,
        "sales_yearly_values": sales_yearly_values,

        # production datasets
        "production_daily_labels": production_daily_labels,
        "production_daily_values": production_daily_values,
        "production_monthly_labels": production_monthly_labels,
        "production_monthly_values": production_monthly_values,
        "production_yearly_labels": production_yearly_labels,
        "production_yearly_values": production_yearly_values,

        # tables
        "top_outstanding": top_outstanding,
        "low_stock": low_stock,
    }
    return render(request, "core/main_dashboard.html", context)


# -------------------------
# EMPLOYEES / LABOUR
# -------------------------
def expense_list(request):
    categories = ExpenseCategory.objects.all().order_by('name')
    expenses = Expense.objects.select_related('category').order_by('-expense_date')

    today = timezone.now().date()
    month_expenses = (
        Expense.objects.filter(expense_date__year=today.year, expense_date__month=today.month)
        .values('category__name')
        .annotate(total=Sum('amount'))
        .order_by('category__name')
    )

    return render(request, 'core/expenses.html', {
        'categories': categories,
        'expenses': expenses,
        'month_expenses': month_expenses,
    })


def employee_list(request):
    employees = Employee.objects.annotate(
        total_attendance=Count('attendance_records')
    ).order_by('name')
    return render(request, 'core/employees.html', {'employees': employees})


def attendance_board(request):
    today = timezone.now().date()
    records = Attendance.objects.filter(date=today).select_related('employee')
    employees = Employee.objects.order_by('name')
    return render(request, 'core/attendance.html', {
        'date': today,
        'records': records,
        'employees': employees,
    })
