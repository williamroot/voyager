from django.urls import path

from . import extrair_proxy, views

urlpatterns = [
    path('', views.root, name='root'),
    # Extração de autos (proxy p/ Zordon; paths espelhados 1:1 com o HTML servido lá)
    path('extrair', extrair_proxy.extrair, name='extrair-autos'),
    path('api/extrair/modelos', extrair_proxy.api_modelos, name='extrair-modelos'),
    path('extrair/<uuid:job_id>', extrair_proxy.status, name='extrair-status'),
    path('extrair/<uuid:job_id>/reprocessar', extrair_proxy.reprocessar, name='extrair-reprocessar'),
    path('api/extrair/<uuid:job_id>', extrair_proxy.api_status, name='extrair-api'),
]
