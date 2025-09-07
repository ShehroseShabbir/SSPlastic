# polyroll_mgmt/urls.py
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from core import views as core_views  # your dashboard view

urlpatterns = [
    path('admin/', admin.site.urls),                      # uses your SSPAdminSite if configured
    path('adnmin/', include('django.contrib.auth.urls')),

    path('', core_views.dashboard, name='dashboard'),     # protect with @login_required
    path('', include('core.urls')),                       # app routes
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
