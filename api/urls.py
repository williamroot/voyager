from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from . import busca_views
from . import diarios_views
from . import leads as leads_views
from . import monitoring_views
from .viewsets import (
    HealthLivenessView,
    HealthReadinessView,
    IngestionRunViewSet,
    MovimentacaoViewSet,
    ProcessViewSet,
    TribunalViewSet,
)

router = DefaultRouter()
router.register('tribunais', TribunalViewSet, basename='tribunal')
router.register('processos', ProcessViewSet, basename='processo')
router.register('movimentacoes', MovimentacaoViewSet, basename='movimentacao')
router.register('ingestion-runs', IngestionRunViewSet, basename='ingestion-run')

urlpatterns = [
    path('', include(router.urls)),
    path('health/', HealthReadinessView.as_view({'get': 'list'}), name='health'),
    path('health/liveness/', HealthLivenessView.as_view({'get': 'list'}), name='health-liveness'),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),
    path('leads/', leads_views.listar_leads, name='leads-list'),
    path('leads/consumed/', leads_views.marcar_consumidos, name='leads-consumed'),
    path('leads/stats/', leads_views.stats, name='leads-stats'),

    # API de Busca v1 (100% Elasticsearch — ver search/busca_api.py)
    path('busca/processos/', busca_views.busca_processos, name='busca-processos'),
    path('busca/processos/<str:cnj>/', busca_views.busca_processo, name='busca-processo'),
    path('busca/processos/<str:cnj>/movimentacoes/',
         busca_views.busca_processo_movimentacoes, name='busca-processo-movs'),
    path('busca/movimentacoes/', busca_views.busca_movimentacoes, name='busca-movimentacoes'),

    # Diários Oficiais (Jusbrasil/Digesto-compat)
    path('diarios-oficiais/doc/buscar', diarios_views.diario_buscar, name='diario-buscar'),
    path('diarios-oficiais/doc/get/<int:doc_id>', diarios_views.diario_get, name='diario-get'),
    path('diarios-oficiais/fontes_recortes', diarios_views.fontes_recortes, name='fontes-recortes'),
    path('monitoramento/proc/tipos_norm_andamentos_movs', diarios_views.tipos_norm, name='tipos-norm'),
    path('base-judicial/tribproc/status_cobertura', diarios_views.status_cobertura, name='status-cobertura'),

    # Monitoramento (Jusbrasil/Digesto-compat) — Fase F
    path('monitoramento/monitored_term', monitoring_views.monitored_term_list, name='monitored-term-list'),
    path('monitoramento/monitored_term/<int:pk>', monitoring_views.monitored_term_detail, name='monitored-term-detail'),
    path('monitoramento/monitored_person', monitoring_views.monitored_person_list, name='monitored-person-list'),
    path('monitoramento/proc', monitoring_views.monitored_process_list, name='monitored-process-list'),
    path('monitoramento/detections', monitoring_views.detection_list, name='detection-list'),
]
