from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from collections import defaultdict
from decimal import Decimal
from .models import Customer, Order, OrderItem, Expense, Payment, ExpenseCategory, Employee, Attendance, CustomerMaterialLedger
from .utils import generate_invoice, send_invoice_email
from django.db.models import (
    Sum, F, Value, ExpressionWrapper, DecimalField, OuterRef, Subquery, Count, Q
)
from django.db.models.functions import Coalesce, TruncMonth, TruncWeek, TruncDate
from django.utils import timezone
import datetime, csv


MONEY = DecimalField(max_digits=18, decimal_places=2)
KG    = DecimalField(max_digits=12, decimal_places=3)

def _orders_with_totals(qs):
    item_amount_expr = ExpressionWrapper(
        F('roll_weight') * F('quantity') * F('price_per_kg'),
        output_field=MONEY,
    )
    items_total_sq = (
        OrderItem.objects.filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum(item_amount_expr), Value(0, output_field=MONEY)))
        .values('total')[:1]
    )
    payments_total_sq = (
        Payment.objects.filter(order_id=OuterRef('pk'))
        .values('order_id')
        .annotate(total=Coalesce(Sum('amount'), Value(0, output_field=MONEY)))
        .values('total')[:1]
    )
    return qs.annotate(
        total_amount_calc=Coalesce(Subquery(items_total_sq, output_field=MONEY), Value(0, output_field=MONEY)),
        total_paid_calc=Coalesce(Subquery(payments_total_sq, output_field=MONEY), Value(0, output_field=MONEY)),
    )

def customer_balances(request):
    # --- FIX: make the zero a DecimalField using output_field=KG ---
    material_by_customer = dict(
        CustomerMaterialLedger.objects
        .values('customer_id')
        .annotate(balance=Coalesce(Sum('delta_kg'), Value(0, output_field=KG)))  # <-- typed 0
        .values_list('customer_id', 'balance')
    )

    # 2) Outstanding = sum over orders (total - paid), computed per order safely
    outstanding_by_customer = defaultdict(Decimal)
    orders = _orders_with_totals(Order.objects.select_related('customer').only('id', 'customer_id'))
    for o in orders:
        outstanding_by_customer[o.customer_id] += (o.total_amount_calc or Decimal('0')) - (o.total_paid_calc or Decimal('0'))

    # 3) Build rows
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

def dashboard(request):
    today = timezone.localdate()

    # ---------- MONEY & OPEN ORDERS (skip DRAFT) ----------
    base_orders = Order.objects.exclude(status='DRAFT').select_related('customer').only(
        'id','invoice_number','customer_id','order_date','delivery_date','status'
    )
    orders_annot = _orders_with_totals(base_orders)

    order_rows = []
    pending_sum = Decimal('0')
    open_orders = []
    for o in orders_annot:
        outstanding = (o.total_amount_calc or Decimal('0')) - (o.total_paid_calc or Decimal('0'))
        order_rows.append((o, outstanding))
        if outstanding > 0:
            pending_sum += outstanding
            open_orders.append((o, outstanding))

    overdue_orders = sum(1 for o, out in order_rows if out > 0 and o.delivery_date and o.delivery_date < today)

    # Sales per Month (sum of order totals by order_date)
    sales_per_month = defaultdict(Decimal)
    for o, _out in order_rows:
        if o.order_date:
            key = o.order_date.strftime("%b-%Y")
            sales_per_month[key] += (o.total_amount_calc or Decimal('0'))

    # Payment status counts
    paid_count    = sum(1 for _o, out in order_rows if out <= 0)
    partial_count = sum(1 for _o, out in order_rows if 0 < out < (_o.total_amount_calc or Decimal('0')))
    pending_count = sum(1 for _o, out in order_rows if (_o.total_paid_calc or Decimal('0')) == 0 and (_o.total_amount_calc or 0) > 0)

    # Outstanding by customer (top N)
    customers = {c.id: c for c in Customer.objects.all()}
    outstanding_by_customer = defaultdict(Decimal)
    for o, out in order_rows:
        outstanding_by_customer[o.customer_id] += out
    top_outstanding = sorted(
        ({"id": cid, "company_name": customers.get(cid).company_name if customers.get(cid) else f"Customer {cid}", "amount": amt}
         for cid, amt in outstanding_by_customer.items() if amt > 0),
        key=lambda r: r["amount"], reverse=True
    )[:8]

    # ---------- MATERIAL IN vs OUT (Monthly) ----------
    # IN is positive, OUT is negative in ledger. We’ll present OUT as positive values in chart.
    material_qs = (
        CustomerMaterialLedger.objects
        .annotate(m=TruncMonth('date'))
        .values('m', 'type')
        .annotate(kg=Coalesce(Sum('delta_kg'), Value(0, output_field=KG)))
        .order_by('m')
    )
    mat_in = defaultdict(Decimal)
    mat_out = defaultdict(Decimal)
    for row in material_qs:
        key = row['m'].strftime("%b-%Y") if row['m'] else "Unknown"
        if row['type'] == 'IN':
            mat_in[key] += Decimal(row['kg'] or 0)
        else:
            # row['kg'] is negative for OUT; flip sign for visualization
            mat_out[key] += -Decimal(row['kg'] or 0)

    # Align month labels across both series
    all_months = sorted(set(mat_in.keys()) | set(mat_out.keys()),
                        key=lambda s: timezone.datetime.strptime(s, "%b-%Y"))
    material_month_labels = all_months
    material_in_values  = [float(mat_in.get(m, 0)) for m in all_months]
    material_out_values = [float(mat_out.get(m, 0)) for m in all_months]

    # ---------- PRODUCTION KG (Weekly & Daily) ----------
    # Production kg = sum(OrderItem.roll_weight * quantity). We’ll compute on order_date.
    kg_expr = ExpressionWrapper(F('roll_weight') * F('quantity'), output_field=KG)
    # Weekly (last 12 weeks)
    twelve_weeks_ago = today - timezone.timedelta(weeks=12)
    prod_week_qs = (
        OrderItem.objects
        .filter(order__order_date__gte=twelve_weeks_ago)
        .annotate(w=TruncWeek('order__order_date'))
        .values('w')
        .annotate(kg=Coalesce(Sum(kg_expr), Value(0, output_field=KG)))
        .order_by('w')
    )
    production_week_labels = [row['w'].strftime("%Y-%W") for row in prod_week_qs if row['w']]
    production_week_values = [float(row['kg'] or 0) for row in prod_week_qs if row['w']]

    # Daily (last 14 days) — SQLite-safe: group by the date field directly
    fourteen_days_ago = today - timezone.timedelta(days=14)
    prod_day_qs = (
        OrderItem.objects
        .filter(order__order_date__gte=fourteen_days_ago)
        .values('order__order_date')  # <-- no TruncDate
        .annotate(kg=Coalesce(Sum(kg_expr), Value(0, output_field=KG)))
        .order_by('order__order_date')
    )

    production_day_labels = [
        row['order__order_date'].strftime("%m-%d")
        for row in prod_day_qs
        if row['order__order_date']
    ]
    production_day_values = [float(row['kg'] or 0) for row in prod_day_qs]

    # ---------- Material balances & open orders table ----------
    material_balances = dict(
        CustomerMaterialLedger.objects.values('customer_id')
        .annotate(balance=Coalesce(Sum('delta_kg'), Value(0, output_field=KG)))
        .values_list('customer_id', 'balance')
    )
    low_stock = sorted(
        ({"id": c.id, "company_name": c.company_name, "balance": Decimal(material_balances.get(c.id, 0) or 0)}
         for c in customers.values()),
        key=lambda r: r["balance"]
    )[:8]

    open_orders_sorted = sorted(open_orders, key=lambda t: t[0].delivery_date or today)
    open_orders_rows = [
        {
            "invoice": o.invoice_number,
            "customer": o.customer.company_name if o.customer_id in customers else "",
            "total": o.total_amount_calc,
            "outstanding": out,
            "delivery": o.delivery_date,
            "order_id": o.id,
        }
        for o, out in open_orders_sorted[:10]
    ]

    context = {
        "cards": {
            "open_orders": len(open_orders),
            "pending_payments": pending_sum,   # DRAFT excluded
            "overdue_orders": overdue_orders,
            "total_material": sum(Decimal(v or 0) for v in material_balances.values()),
        },
        "open_orders_rows": open_orders_rows,
        "sales_labels": list(sales_per_month.keys()),
        "sales_values": [float(v) for v in sales_per_month.values()],
        "status_counts": {"paid": paid_count, "partial": partial_count, "pending": pending_count},

        # New datasets
        "material_month_labels": material_month_labels,
        "material_in_values": material_in_values,
        "material_out_values": material_out_values,
        "production_week_labels": production_week_labels,
        "production_week_values": production_week_values,
        "production_day_labels": production_day_labels,
        "production_day_values": production_day_values,

        "top_outstanding": top_outstanding,
        "low_stock": low_stock,
    }
    return render(request, "core/main_dashboard.html", context)

def customer_statement(request, customer_id):
    customer = get_object_or_404(Customer, id=customer_id)
    orders = customer.order_set.all()
    return render(request, 'core/customer_statement.html', {'customer': customer, 'orders': orders})



# -------------------------
# EMPLOYEES / LABOUR
# -------------------------
def expense_list(request):
    # list + quick totals by category and this month
    categories = ExpenseCategory.objects.all().order_by('name')
    expenses = Expense.objects.select_related('category').order_by('-expense_date')

    # monthly totals (current month)
    today = timezone.now().date()
    month_expenses = Expense.objects.filter(
        expense_date__year=today.year,
        expense_date__month=today.month
    ).values('category__name').annotate(total=Sum('amount')).order_by('category__name')

    context = {
        'categories': categories,
        'expenses': expenses,
        'month_expenses': month_expenses,
    }
    return render(request, 'core/expenses.html', context)

def employee_list(request):
    employees = Employee.objects.annotate(
        total_attendance=Count('attendance_records')
    ).order_by('name')
    context = {'employees': employees}
    return render(request, 'core/employees.html', context)

def attendance_board(request):
    # simple board of today’s attendance
    today = timezone.now().date()
    records = Attendance.objects.filter(date=today).select_related('employee')
    employees = Employee.objects.order_by('name')
    context = {
        'date': today,
        'records': records,
        'employees': employees,
    }
    return render(request, 'core/attendance.html', context)
