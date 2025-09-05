from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from . import views



urlpatterns = [
    path('employees/', views.employee_list, name='employee_list'),
    path('expenses/', views.expense_list, name='expense_list'),
    path('attendance/', views.attendance_board, name='attendance_board'),
    path('reports/customer-balances/', views.customer_balances, name='customer_balances'),
]  



if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)