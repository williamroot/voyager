"""Queries agregadas usadas pelo dashboard."""
from datetime import date, timedelta

from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from tribunals.models import IngestionRun, Movimentacao, Process, SchemaDriftAlert, Tribunal


def kpis_globais() -> dict:
    agora = timezone.now()
    cutoff_24h = agora - timedelta(hours=24)
    cutoff_48h = agora - timedelta(hours=48)
    movs_24h = Movimentacao.objects.filter(inserido_em__gte=cutoff_24h).count()
    movs_24_48h = Movimentacao.objects.filter(inserido_em__gte=cutoff_48h, inserido_em__lt=cutoff_24h).count()
    delta_pct = None
    if movs_24_48h:
        delta_pct = round((movs_24h - movs_24_48h) / movs_24_48h * 100, 1)
    return {
        'total_processos': Process.objects.count(),
        'total_movimentacoes': Movimentacao.objects.count(),
        'movs_24h': movs_24h,
        'movs_24h_delta_pct': delta_pct,
        'cancelados': Movimentacao.objects.filter(ativo=False).count(),
        'ultima_atualizacao': IngestionRun.objects
            .filter(status=IngestionRun.STATUS_SUCCESS).order_by('-finished_at')
            .values_list('finished_at', flat=True).first(),
        'drift_abertos': SchemaDriftAlert.objects.filter(resolvido=False).count(),
    }


def sparkline_24h(tribunal_sigla: str | None = None) -> list[int]:
    """Conta movimentações inseridas por hora nas últimas 24h."""
    agora = timezone.now()
    qs = Movimentacao.objects.filter(inserido_em__gte=agora - timedelta(hours=24))
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    pontos = [0] * 24
    for inserido in qs.values_list('inserido_em', flat=True).iterator(chunk_size=5000):
        delta_h = int((agora - inserido).total_seconds() // 3600)
        if 0 <= delta_h < 24:
            pontos[23 - delta_h] += 1
    return pontos


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


def distribuicao_por_meio(tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(meio_completo='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = qs.values('meio_completo').annotate(total=Count('id')).order_by('-total')[:8]
    return [{'meio': r['meio_completo'] or 'Não informado', 'total': r['total']} for r in rows]


def top_tipos_comunicacao(limit: int = 15, tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(tipo_comunicacao='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = qs.values('tipo_comunicacao').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'tipo': r['tipo_comunicacao'], 'total': r['total']} for r in rows]


def top_classes(limit: int = 10, tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(nome_classe='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = qs.values('nome_classe').annotate(total=Count('id')).order_by('-total')[:limit]
    return [{'classe': r['nome_classe'], 'total': r['total']} for r in rows]


def top_orgaos(limit: int = 10, tribunal_sigla: str | None = None) -> list[dict]:
    qs = Movimentacao.objects.exclude(nome_orgao='')
    if tribunal_sigla:
        qs = qs.filter(tribunal_id=tribunal_sigla)
    rows = qs.values('nome_orgao').annotate(total=Count('id')).order_by('-total')[:limit]
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


def filtros_movimentacoes() -> dict:
    """Faceta para chips/filtros dinâmicos: top valores de cada campo categórico."""
    return {
        'tipos': [r['tipo_comunicacao'] for r in
                  Movimentacao.objects.exclude(tipo_comunicacao='')
                  .values('tipo_comunicacao').annotate(n=Count('id')).order_by('-n')[:8]],
        'meios': [r['meio_completo'] for r in
                  Movimentacao.objects.exclude(meio_completo='')
                  .values('meio_completo').annotate(n=Count('id')).order_by('-n')[:6]],
        'classes': [r['nome_classe'] for r in
                    Movimentacao.objects.exclude(nome_classe='')
                    .values('nome_classe').annotate(n=Count('id')).order_by('-n')[:6]],
    }
