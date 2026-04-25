"""Queries agregadas usadas pelo dashboard. Centralizadas para facilitar troca por
materialized views no futuro."""
from datetime import date, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from tribunals.models import IngestionRun, Movimentacao, Process, SchemaDriftAlert, Tribunal


def kpis_globais() -> dict:
    agora = timezone.now()
    cutoff_24h = agora - timedelta(hours=24)
    return {
        'total_processos': Process.objects.count(),
        'total_movimentacoes': Movimentacao.objects.count(),
        'movs_24h': Movimentacao.objects.filter(inserido_em__gte=cutoff_24h).count(),
        'ultima_atualizacao': IngestionRun.objects
            .filter(status=IngestionRun.STATUS_SUCCESS).order_by('-finished_at')
            .values_list('finished_at', flat=True).first(),
        'drift_abertos': SchemaDriftAlert.objects.filter(resolvido=False).count(),
    }


def volume_diario(dias: int = 90, tribunal_sigla: str | None = None) -> list[dict]:
    inicio = date.today() - timedelta(days=dias)
    qs = Movimentacao.objects.filter(data_disponibilizacao__date__gte=inicio)
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = (
        qs.annotate(dia=TruncDate('data_disponibilizacao'))
        .values('dia', 'tribunal_id')
        .annotate(total=Count('id'))
        .order_by('dia', 'tribunal_id')
    )
    return [{'dia': r['dia'].isoformat(), 'tribunal': r['tribunal_id'], 'total': r['total']} for r in rows]


def distribuicao_por_tribunal() -> list[dict]:
    rows = (
        Movimentacao.objects.values('tribunal_id')
        .annotate(total=Count('id'))
        .order_by('-total')
    )
    return [{'tribunal': r['tribunal_id'], 'total': r['total']} for r in rows]


def top_tipos_comunicacao(limit: int = 15, tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(tipo_comunicacao='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = (
        qs.values('tipo_comunicacao').annotate(total=Count('id'))
        .order_by('-total')[:limit]
    )
    return [{'tipo': r['tipo_comunicacao'], 'total': r['total']} for r in rows]


def top_orgaos(limit: int = 10, tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(nome_orgao='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = (
        qs.values('nome_orgao').annotate(total=Count('id'))
        .order_by('-total')[:limit]
    )
    return [{'orgao': r['nome_orgao'], 'total': r['total']} for r in rows]


def runs_recentes(limit: int = 30) -> list[IngestionRun]:
    return list(
        IngestionRun.objects.select_related('tribunal').order_by('-started_at')[:limit]
    )


def cobertura_temporal(tribunal: Tribunal) -> dict:
    runs = (
        IngestionRun.objects.filter(tribunal=tribunal, status=IngestionRun.STATUS_SUCCESS)
        .order_by('janela_inicio').values('janela_inicio', 'janela_fim')
    )
    return {
        'inicio': tribunal.data_inicio_disponivel.isoformat() if tribunal.data_inicio_disponivel else None,
        'fim': date.today().isoformat(),
        'janelas_ok': [{'inicio': r['janela_inicio'].isoformat(), 'fim': r['janela_fim'].isoformat()} for r in runs],
    }
