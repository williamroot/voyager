from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView
from django.templatetags.static import static as static_url

urlpatterns = [
    path('admin/', admin.site.urls),
    path('django-rq/', include('django_rq.urls')),
    path('', include('django_prometheus.urls')),
    path('api/v1/', include('api.urls')),
    path('dashboard/', include('dashboard.urls')),
    path('', include('accounts.urls')),
    path('favicon.ico', RedirectView.as_view(url=static_url('dashboard/favicon.svg'), permanent=True)),
    path('', include('dashboard.urls_root')),
]

# Em DEBUG, expõe rotas pra previsualizar templates de erro durante desenvolvimento.
if settings.DEBUG:
    urlpatterns += [
        path('__preview/404/', TemplateView.as_view(template_name='404.html')),
        path('__preview/403/', TemplateView.as_view(template_name='403.html')),
        path('__preview/500/', TemplateView.as_view(template_name='500.html')),
        path('__preview/400/', TemplateView.as_view(template_name='400.html')),
    ]

# Django carrega esses automaticamente quando DEBUG=False; só explicitamos
# os names para deixar claro o contrato.
handler404 = 'django.views.defaults.page_not_found'
handler500 = 'django.views.defaults.server_error'
handler403 = 'django.views.defaults.permission_denied'
handler400 = 'django.views.defaults.bad_request'
