from django.urls import path

from . import extrair_proxy, views

urlpatterns = [
    path('', views.root, name='root'),
    # Extração de autos (proxy p/ Zordon; paths espelhados 1:1 com o HTML servido lá)
    path('extrair', extrair_proxy.extrair, name='extrair-autos'),
    path('analisar', extrair_proxy.analisar, name='analisar-autos'),
    path('analises', extrair_proxy.analises, name='analises'),
    path('api/analises', extrair_proxy.analises_api, name='analises-api'),
    path('api/analisar/vetorizados', extrair_proxy.analisar_vetorizados, name='analisar-vetorizados'),
    path('api/extrair/modelos', extrair_proxy.api_modelos, name='extrair-modelos'),
    path('extrair/<uuid:job_id>', extrair_proxy.status, name='extrair-status'),
    path('extrair/<uuid:job_id>/raw', extrair_proxy.status_raw, name='extrair-status-raw'),
    path('extrair/<uuid:job_id>/reprocessar', extrair_proxy.reprocessar, name='extrair-reprocessar'),
    path('extrair/<uuid:job_id>/arquivo', extrair_proxy.arquivo, name='extrair-arquivo'),
    path('extrair/<uuid:job_id>/dossie', extrair_proxy.dossie, name='extrair-dossie'),
    path('extrair/<uuid:job_id>/chat', extrair_proxy.chat, name='extrair-chat'),
    path('api/extrair/<uuid:job_id>/jurimetria', extrair_proxy.jurimetria, name='extrair-jurimetria'),
    path('api/extrair/<uuid:job_id>', extrair_proxy.api_status, name='extrair-api'),
]
