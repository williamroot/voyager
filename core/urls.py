from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('django-rq/', include('django_rq.urls')),
    path('', include('django_prometheus.urls')),
    path('api/v1/', include('api.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('', include('dashboard.urls_root')),
]
