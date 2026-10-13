"""API de leads — consumida por Juriscope.

Auth: header `X-API-Key: <key>`. Cada cliente (Juriscope) tem ApiClient
com chave única; chave inválida → 403.

Endpoints:
  GET  /api/leads/            — lista próximos N leads não consumidos
                                (servida do cache pré-calculado quando ele é
                                recente — ver `api/leads_cache.py`)
  POST /api/leads/consumed/   — marca processos como consumidos
  GET  /api/leads/stats/      — métricas agregadas
"""
from __future__ import annotations

import logging
import uuid

import django_rq
from rq import Retry

from django.conf import settings
from django.db import OperationalError
from django.db.models import Count
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from tribunals.jobs import registrar_consumo_leads
from tribunals.models import (
    ApiClient, ClassificadorVersao, LeadConsumption, Process,
)

from . import leads_cache
from .leads_cache import executar_com_teto

logger = logging.getLogger(__name__)


def _autenticar(request) -> ApiClient | None:
    """Lê X-API-Key e devolve ApiClient ativo, senão None."""
    key = request.META.get('HTTP_X_API_KEY') or request.headers.get('X-API-Key')
    if not key:
        return None
    return ApiClient.objects.filter(api_key=key, ativo=True).first()


def _linhas_com_teto(qs, limit: int, segundos: int) -> list:
    """Materializa `qs[:limit]` sob `statement_timeout` de verdade.

    Sem teto, uma consulta ruim aqui não falha: ela SEGURA o worker do gunicorn
    até o arbiter matá-lo (30 s), e a query continua queimando I/O no Postgres
    depois que o cliente já foi embora. Foi o que aconteceu com o
    `PRE_PRECATORIO` do TJSP em 09/09/2026 — três vezes por dia, todo dia.

    O `SET LOCAL` mora em `leads_cache.executar_com_teto` (pgbouncer em
    transaction-mode: o `SET` tem de estar na MESMA transação da query).
    """
    return executar_com_teto(lambda: list(qs[:limit]), segundos)


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
    # devedor_publico=true → só leads cujo devedor é a Fazenda (classe de
    # Cumprimento/Execução contra Fazenda Pública, ou nome com 'fazenda públ').
    # Pensado pro DIREITO_CREDITORIO do TJSP, onde ~94% são execuções privadas
    # (classe 156) que nunca viram precatório — sem isto o cliente gasta o cap
    # de pull/scrape com lixo. Aplicável a qualquer nível (no-op se já é só
    # Fazenda, ex.: PRECATORIO).
    devedor_publico = (request.query_params.get('devedor_publico') or '').lower() in ('1', 'true', 'sim')

    if nivel not in dict(Process.CLASSIF_CHOICES):
        return Response({'erro': f'nivel inválido: {nivel}'}, status=400)

    teto = settings.LEADS_SQL_TIMEOUT_SECONDS
    fonte = 'ao_vivo'
    meta_cache = None
    try:
        servido = None
        if not incluir_consumidos:
            # Registra a combinação pedida: é por ela que o pré-cálculo sabe o
            # que calcular (`api/leads_cache.py`). O `limit` não entra — o
            # Juriscope varia o limite do N2 conforme o que sobrou do N1.
            leads_cache.registrar_pedido(
                cliente.id, leads_cache.chave_combo(nivel, tribunal, min_score,
                                                    devedor_publico))
            if leads_cache.servir_cache_ligado():
                servido = leads_cache.servir_do_cache(
                    cliente.id, nivel, tribunal, min_score, devedor_publico,
                    limit, teto)
        if servido is not None:
            rows, meta_cache = servido
            fonte = 'cache'
        else:
            qs = leads_cache.montar_queryset(cliente.id, nivel, tribunal, min_score,
                                             devedor_publico, incluir_consumidos)
            rows = _linhas_com_teto(qs, limit, teto)
    except OperationalError as exc:
        # 503 e não 500: para o Juriscope isto é falha TRANSITÓRIA (ele pula a
        # rodada e tenta de novo), e o log fica com o recorte que reproduz.
        logger.warning(
            'listar_leads estourou %ss (nivel=%s tribunal=%s limit=%s '
            'min_score=%s devedor_publico=%s fonte=%s): %s',
            teto, nivel, tribunal or '-', limit, min_score, devedor_publico,
            fonte, exc)
        return Response(
            {'erro': f'consulta excedeu o teto de {teto}s no banco',
             'nivel': nivel, 'tribunal': tribunal or None},
            status=status.HTTP_503_SERVICE_UNAVAILABLE)

    # Uma linha por pedido: é por ela que o runbook prova que o pull das 08:00
    # saiu do cache (o Juriscope não guarda o campo `fonte`).
    logger.info(
        'listar_leads fonte=%s cliente=%s nivel=%s tribunal=%s min_score=%s '
        'devedor_publico=%s limit=%s count=%s cache_idade_s=%s',
        fonte, cliente.id, nivel, tribunal or '-', min_score, devedor_publico,
        limit, len(rows), meta_cache['idade_s'] if meta_cache else '-')

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
        # `cache`: pré-calculado por `precomputar_leads_cache` (ver
        # `api/leads_cache.py`); `ao_vivo`: a consulta sob o teto de 20 s.
        'fonte': fonte,
        'cache_idade_s': meta_cache['idade_s'] if meta_cache else None,
        'cache': meta_cache,
        'results': results,
    })


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def marcar_consumidos(request):
    """POST /api/leads/consumed/ {lote_id, consumos: [{cnj, resultado}, ...]}"""
    cliente = _autenticar(request)
    if not cliente:
        return Response({'erro': 'X-API-Key inválida ou ausente'}, status=403)

    lote_id = (request.data.get('lote_id') or '').strip()
    consumos = request.data.get('consumos') or []
    if not lote_id:
        return Response({'erro': 'lote_id obrigatório'}, status=400)
    try:
        uuid.UUID(lote_id)
    except (ValueError, TypeError, AttributeError):
        return Response({'erro': 'lote_id deve ser um UUID válido'}, status=400)
    if not isinstance(consumos, list) or not consumos:
        return Response({'erro': 'consumos deve ser lista não-vazia'}, status=400)
    if len(consumos) > 5000:
        return Response({'erro': 'consumos excede 5000 por requisição'}, status=400)

    try:
        django_rq.get_queue('leads_consumo').enqueue(
            registrar_consumo_leads, cliente.id, consumos, lote_id,
            job_timeout=1800, result_ttl=86400, failure_ttl=604800,
            retry=Retry(max=3, interval=[30, 120, 600]),
        )
    except Exception:
        logger.exception('leads_consumo enqueue falhou lote=%s cliente=%s', lote_id, cliente.id)
        return Response({'erro': 'fila indisponível, tente novamente'}, status=503)
    return Response({'enfileirado': True, 'lote_id': lote_id,
                     'recebidos': len(consumos)}, status=202)


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
