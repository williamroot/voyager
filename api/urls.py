from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

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
]
