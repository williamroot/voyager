"""Endpoints REST do Mapa Comercial de Precatórios.

Views JSON que servem o mapa (choropleth + drill-down + ranking) a partir do
serviço de agregação `search.agg_comercial` (100% Elasticsearch). Todos os
endpoints exigem sessão logada — é dado sensível de negócio.

O CONTRATO JSON completo (request/response) está documentado no topo de
`search/agg_comercial.py`. Estas views só fazem: parse dos filtros → chamada do
serviço → `JsonResponse`. Erros de infra (ES fora) viram 503 (nunca 500 cru);
`uf` ausente no drill-down vira 400.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from search import agg_comercial, agg_estado as agg_estado_svc

logger = logging.getLogger('voyager.comercial.views')


def _json(payload, status=200):
    # default=str garante serialização de qualquer resíduo (datas etc.)
    return JsonResponse(payload, status=status, json_dumps_params={'default': str})


@never_cache
@login_required
@require_GET
def comercial_mapa_page(request):
    """GET /dashboard/comercial/mapa/ — página HTML do Mapa Comercial.

    Só renderiza o shell; TODOS os dados vêm dos 3 endpoints JSON
    (`comercial-mapa`/`comercial-tribunais`/`comercial-top`) via fetch no browser.
    Nenhuma query pesada aqui — o Postgres não é tocado.

    `@never_cache` (padrão já usado em dashboard/views.py): sem ele o browser
    servia o HTML do disco e o usuário continuava vendo a versão ANTIGA depois do
    deploy — aconteceu de verdade (12/08/2026: o print reclamando de layout
    mostrava textos já removidos há 3 deploys). A página é toda dinâmica e o
    payload é pequeno; cachear HTML aqui não economiza nada e esconde correção.
    """
    return render(request, 'dashboard/comercial_mapa.html')


@login_required
@require_GET
def comercial_mapa(request):
    """GET /dashboard/api/comercial/mapa — agregado por UF + total + score de foco."""
    filtros = agg_comercial.parse_filtros(request.GET)
    try:
        payload = agg_comercial.agg_por_uf(filtros)
    except Exception:
        logger.exception('comercial_mapa: falha na agregação ES', extra={'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_tribunais(request):
    """GET /dashboard/api/comercial/tribunais?uf=SP — drill-down por tribunal."""
    filtros = agg_comercial.parse_filtros(request.GET)
    uf = filtros.get('uf')
    if not uf:
        return _json({'erro': 'parâmetro uf é obrigatório'}, status=400)
    # `uf` já está nos filtros; agg_tribunais reforça — remove p/ não duplicar semântica
    filtros_sem_uf = {k: v for k, v in filtros.items() if k != 'uf'}
    try:
        payload = agg_comercial.agg_tribunais(uf, filtros_sem_uf)
    except Exception:
        logger.exception('comercial_tribunais: falha na agregação ES',
                         extra={'uf': uf, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_estado(request, uf):
    """GET /dashboard/api/comercial/estado/<uf>/ — página dedicada de um estado.

    Mesmos filtros do mapa (`agg_comercial.parse_filtros`) + `metrica`
    (possiveis|confirmados|todos). O `uf` da querystring é ignorado — manda o da
    rota. Contrato JSON completo no topo de `search/agg_estado.py`.
    UF inválida → 400; ES fora → 503 (nunca 500 cru).
    """
    filtros = agg_comercial.parse_filtros(request.GET)
    metrica = request.GET.get('metrica', agg_estado_svc.METRICA_DEFAULT)
    try:
        payload = agg_estado_svc.agg_estado(uf, filtros, metrica)
    except agg_estado_svc.UfInvalida:
        return _json({'erro': f'UF inválida: {uf}'}, status=400)
    except Exception:
        logger.exception('comercial_estado: falha na agregação ES',
                         extra={'uf': uf, 'metrica': metrica, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)


@login_required
@require_GET
def comercial_top(request):
    """GET /dashboard/api/comercial/top?metric=score&n=10 — ranking Top-N por UF."""
    filtros = agg_comercial.parse_filtros(request.GET)
    metric = request.GET.get('metric', 'score')
    n = request.GET.get('n', 10)
    try:
        payload = agg_comercial.ranking_top(metric, n, filtros)
    except Exception:
        logger.exception('comercial_top: falha na agregação ES',
                         extra={'metric': metric, 'filtros': filtros})
        return _json({'erro': 'falha ao consultar o índice de busca'}, status=503)
    return _json(payload)
