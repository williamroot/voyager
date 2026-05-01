"""API de leads — consumida por Juriscope.

Auth: header `X-API-Key: <key>`. Cada cliente (Juriscope) tem ApiClient
com chave única; chave inválida → 403.

Endpoints:
  GET  /api/leads/            — lista próximos N leads não consumidos
  POST /api/leads/consumed/   — marca processos como consumidos
  GET  /api/leads/stats/      — métricas agregadas
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from tribunals.models import (
    ApiClient, ClassificadorVersao, LeadConsumption, Process,
)


def _autenticar(request) -> ApiClient | None:
    """Lê X-API-Key e devolve ApiClient ativo, senão None."""
    key = request.META.get('HTTP_X_API_KEY') or request.headers.get('X-API-Key')
    if not key:
        return None
    return ApiClient.objects.filter(api_key=key, ativo=True).first()


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def listar_leads(request):
    """GET /api/leads/?nivel=PRECATORIO&tribunal=TRF1&limit=5000&min_score=0"""
    cliente = _autenticar(request)
    if not cliente:
        return Response({'erro': 'X-API-Key inválida ou ausente'}, status=403)

    nivel = (request.query_params.get('nivel') or 'PRECATORIO').strip().upper()
    tribunal = (request.query_params.get('tribunal') or '').strip().upper()
    try:
        limit = max(1, min(int(request.query_params.get('limit', 5000)), 10000))
    except (TypeError, ValueError):
        limit = 5000
    try:
        min_score = max(0.0, min(float(request.query_params.get('min_score', 0)), 1.0))
    except (TypeError, ValueError):
        min_score = 0.0
    incluir_consumidos = (request.query_params.get('incluir_consumidos') or '').lower() in ('1', 'true', 'sim')

    if nivel not in dict(Process.CLASSIF_CHOICES):
        return Response({'erro': f'nivel inválido: {nivel}'}, status=400)

    qs = (
        Process.objects.filter(classificacao=nivel,
                               classificacao_score__gte=min_score)
        .select_related('tribunal')
        .order_by('-classificacao_score', '-id')
    )
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal)
    if not incluir_consumidos:
        # Exclui processos que esse cliente já consumiu — qualquer registro
        # em LeadConsumption pra esse cliente x processo conta.
        consumidos = LeadConsumption.objects.filter(cliente=cliente).values('processo_id')
        qs = qs.exclude(pk__in=consumidos)

    rows = list(qs[:limit])
    base_url = 'https://voyager.was.dev.br'  # ajuste se mudar
    results = [
        {
            'cnj': p.numero_cnj,
            'tribunal': p.tribunal_id,
            'classificacao': p.classificacao,
            'score': round(p.classificacao_score or 0, 4),
            'classe_nome': p.classe_nome,
            'classe_codigo': p.classe_codigo,
            'ano_cnj': p.ano_cnj,
            'classificado_em': p.classificacao_em.isoformat() if p.classificacao_em else None,
            'classificacao_versao': p.classificacao_versao,
            'link_voyager': f'{base_url}/dashboard/processos/{p.pk}/',
        }
        for p in rows
    ]
    return Response({
        'count': len(results),
        'limit': limit,
        'nivel': nivel,
        'tribunal': tribunal or None,
        'min_score': min_score,
        'results': results,
    })


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def marcar_consumidos(request):
    """POST /api/leads/consumed/ {consumos: [{cnj, resultado}, ...]}"""
    cliente = _autenticar(request)
    if not cliente:
        return Response({'erro': 'X-API-Key inválida ou ausente'}, status=403)

    consumos = request.data.get('consumos') or []
    if not isinstance(consumos, list):
        return Response({'erro': 'consumos deve ser lista'}, status=400)

    resultados_validos = dict(LeadConsumption.RESULTADO_CHOICES)

    cnjs = []
    resultado_por_cnj = {}
    for c in consumos:
        if not isinstance(c, dict):
            continue
        cnj = (c.get('cnj') or '').strip()
        resultado = (c.get('resultado') or LeadConsumption.RESULTADO_PENDENTE).strip()
        if not cnj or resultado not in resultados_validos:
            continue
        cnjs.append(cnj)
        resultado_por_cnj[cnj] = resultado

    if not cnjs:
        return Response({'criados': 0, 'duplicados': 0, 'nao_encontrados': []})

    procs = list(Process.objects.filter(numero_cnj__in=cnjs).only('id', 'numero_cnj'))
    proc_por_cnj = {p.numero_cnj: p for p in procs}
    nao_encontrados = [c for c in cnjs if c not in proc_por_cnj]

    a_criar = []
    for cnj, p in proc_por_cnj.items():
        a_criar.append(LeadConsumption(
            processo=p, cliente=cliente,
            resultado=resultado_por_cnj[cnj],
        ))
    criados_objs = LeadConsumption.objects.bulk_create(a_criar)
    return Response({
        'criados': len(criados_objs),
        'nao_encontrados': nao_encontrados,
    }, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def stats(request):
    """GET /api/leads/stats/ — métricas pra esse cliente."""
    cliente = _autenticar(request)
    if not cliente:
        return Response({'erro': 'X-API-Key inválida ou ausente'}, status=403)

    consumidos_pids = LeadConsumption.objects.filter(cliente=cliente).values('processo_id')

    pending = {
        nivel: Process.objects.filter(classificacao=nivel)
                              .exclude(pk__in=consumidos_pids).count()
        for nivel in (Process.CLASSIF_PRECATORIO, Process.CLASSIF_PRE_PRECATORIO,
                      Process.CLASSIF_DIREITO_CREDITORIO)
    }

    cons_qs = LeadConsumption.objects.filter(cliente=cliente)
    consumidos_total = cons_qs.count()
    hoje = timezone.now().date()
    consumidos_hoje = cons_qs.filter(consumido_em__date=hoje).count()

    by_resultado = dict(cons_qs.values_list('resultado').annotate(n=Count('id')))
    validados = by_resultado.get(LeadConsumption.RESULTADO_VALIDADO, 0)
    taxa_val = (validados / consumidos_total) if consumidos_total else None

    versao_ativa = ClassificadorVersao.objects.filter(ativa=True).first()

    return Response({
        'pending': pending,
        'consumidos_total': consumidos_total,
        'consumidos_hoje': consumidos_hoje,
        'consumidos_por_resultado': by_resultado,
        'validados_total': validados,
        'taxa_validacao': taxa_val,
        'modelo_versao': versao_ativa.versao if versao_ativa else 'v5',
        'modelo_atualizado_em': versao_ativa.criada_em.isoformat() if versao_ativa else None,
    })
