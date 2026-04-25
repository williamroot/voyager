from django.db.models import Count, Max
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

import django_rq
import redis
from django.conf import settings

from tribunals.models import IngestionRun, Movimentacao, Process, SchemaDriftAlert, Tribunal

from .filters import IngestionRunFilter, MovimentacaoFilter, ProcessFilter
from .pagination import DefaultPagination, MovimentacaoCursorPagination
from .serializers import (
    IngestionRunSerializer,
    MovimentacaoDetailSerializer,
    MovimentacaoListSerializer,
    ProcessDetailSerializer,
    ProcessListSerializer,
    TribunalSerializer,
)

LAG_HORAS_LIMITE = 36


class TribunalViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Tribunal.objects.all()
    serializer_class = TribunalSerializer
    lookup_field = 'sigla'

    @action(detail=True, methods=['get'])
    def estatisticas(self, request, sigla=None):
        t = self.get_object()
        ult_run = IngestionRun.objects.filter(tribunal=t).order_by('-started_at').first()
        return Response({
            'sigla': t.sigla,
            'total_processos': Process.objects.filter(tribunal=t).count(),
            'total_movimentacoes': Movimentacao.objects.filter(tribunal=t).count(),
            'ultimo_run': IngestionRunSerializer(ult_run).data if ult_run else None,
            'drift_alerts_abertos': SchemaDriftAlert.objects.filter(tribunal=t, resolvido=False).count(),
        })


class ProcessViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Process.objects.select_related('tribunal').all()
    filterset_class = ProcessFilter
    pagination_class = DefaultPagination
    ordering_fields = ('inserido_em', 'ultima_movimentacao_em', 'total_movimentacoes')

    def get_serializer_class(self):
        return ProcessDetailSerializer if self.action == 'retrieve' else ProcessListSerializer

    def get_object(self):
        lookup = self.kwargs[self.lookup_field]
        try:
            return self.get_queryset().get(pk=int(lookup))
        except (ValueError, Process.DoesNotExist):
            return self.get_queryset().get(numero_cnj=lookup)

    @action(detail=True, methods=['get'])
    def movimentacoes(self, request, pk=None):
        proc = self.get_object()
        qs = Movimentacao.objects.filter(processo=proc).order_by('-data_disponibilizacao', '-id')
        paginator = MovimentacaoCursorPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = MovimentacaoListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class MovimentacaoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Movimentacao.objects.select_related('tribunal', 'processo').all()
    filterset_class = MovimentacaoFilter
    pagination_class = MovimentacaoCursorPagination

    def get_serializer_class(self):
        return MovimentacaoDetailSerializer if self.action == 'retrieve' else MovimentacaoListSerializer


class IngestionRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = IngestionRun.objects.select_related('tribunal').all()
    serializer_class = IngestionRunSerializer
    filterset_class = IngestionRunFilter
    pagination_class = DefaultPagination


class HealthLivenessView(viewsets.ViewSet):
    permission_classes = [AllowAny]

    def list(self, request):
        return Response({'status': 'ok'})


class HealthReadinessView(viewsets.ViewSet):
    """Healthcheck rico — usado por monitoring externo, NÃO por liveness probe.

    Retorna 503 se algum tribunal ativo está com lag > 36h. Drift alerts não
    afetam o status HTTP (apenas aparecem no payload).
    """
    permission_classes = [AllowAny]

    def list(self, request):
        from django.db import connection

        try:
            with connection.cursor() as c:
                c.execute('SELECT 1')
            db_ok = True
        except Exception:
            db_ok = False

        try:
            r = redis.from_url(settings.REDIS_URL, decode_responses=True)
            r.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

        ativos = Tribunal.objects.filter(ativo=True)
        agora = timezone.now()
        tribunais = []
        algum_lag_alto = False
        for t in ativos:
            ult = IngestionRun.objects.filter(tribunal=t).order_by('-started_at').first()
            if ult:
                lag_h = (agora - ult.started_at).total_seconds() / 3600
                tribunais.append({
                    'sigla': t.sigla,
                    'ultimo_run': ult.started_at.isoformat(),
                    'status': ult.status,
                    'lag_horas': round(lag_h, 1),
                })
                if t.backfill_concluido_em and lag_h > LAG_HORAS_LIMITE:
                    algum_lag_alto = True
            else:
                tribunais.append({'sigla': t.sigla, 'ultimo_run': None, 'status': None, 'lag_horas': None})

        drift_count = SchemaDriftAlert.objects.filter(resolvido=False).count()

        try:
            queues = {q: django_rq.get_queue(q).count for q in ('default', 'djen_ingestion', 'djen_backfill')}
        except Exception:
            queues = {}

        body = {
            'db': 'ok' if db_ok else 'fail',
            'redis': 'ok' if redis_ok else 'fail',
            'tribunais': tribunais,
            'drift_alerts_abertos': drift_count,
            'filas': queues,
        }
        http_status = status.HTTP_200_OK
        if not db_ok or not redis_ok or algum_lag_alto:
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(body, status=http_status)
