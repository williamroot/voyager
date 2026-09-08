"""Tela interna da busca por parte ao vivo (`/dashboard/busca-tribunal/`).

Existe para uma coisa: um humano conferir uma fonte sem curl e sem API key —
antes de ligar um tribunal, depois de um tribunal mudar de layout, ou quando
alguém diz "não achou nada" e é preciso ver se a fonte respondeu ou recusou.

Tela NOVA, e não um botão dentro de `/dashboard/busca/`: aquela é a busca no
índice, com cursor, filtros e cobertura — a mecânica desta é outra (assíncrona,
por tribunal, com estados que a outra não tem). Misturar as duas numa tela só
economizaria uma entrada no menu e custaria a clareza das duas.

Reusa as MESMAS funções da API v1 (`api/busca_tribunal_views.py`): o que muda é
só a autenticação (sessão em vez de API key). Duplicar a montagem da resposta
faria a tela e a API divergirem no dia em que uma delas mudasse.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from api.busca_tribunal_views import (
    _resposta_do_run,
    _run_em_cache,
    _tribunais_pedidos,
)
from enrichers.busca.base import ROTULOS
from enrichers.busca.entrada import EntradaInvalida, validar
from enrichers.busca.jobs import iniciar
from enrichers.busca.registry import CATALOGO, catalogo_publico
from tribunals.models import BuscaTribunalRun

logger = logging.getLogger('voyager.dashboard.busca_tribunal')


@login_required
@never_cache
def pagina(request):
    """Shell puro: o dado todo vem por fetch das duas views abaixo."""
    from django.urls import reverse

    return render(request, 'dashboard/busca_tribunal.html', {
        'criar_url': reverse('dashboard:busca-tribunal-criar'),
        'ler_url': reverse('dashboard:busca-tribunal-ler'),
        'criterios': [{'id': cid, 'rotulo': rot} for cid, rot in ROTULOS.items()],
        'fontes': catalogo_publico(),
    })


@login_required
@require_POST
def criar(request):
    """POST {criterio, valor, tribunais[], forcar} -> o run recém-criado."""
    try:
        corpo = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'erro': 'corpo_invalido'}, status=400)

    try:
        entrada = validar(corpo.get('criterio'), corpo.get('valor'))
    except EntradaInvalida as exc:
        return JsonResponse({'erro': exc.codigo, 'mensagem': exc.mensagem}, status=400)

    tribunais = _tribunais_pedidos(corpo.get('tribunais'))
    desconhecidos = [t for t in tribunais if t not in CATALOGO]
    if desconhecidos:
        return JsonResponse({
            'erro': 'tribunal_sem_busca',
            'mensagem': f'Sem busca por parte em: {", ".join(desconhecidos)}.',
        }, status=400)

    if not corpo.get('forcar'):
        anterior = _run_em_cache(entrada['criterio'], entrada['normalizado'], tribunais)
        if anterior:
            return JsonResponse(_resposta_do_run(anterior, em_cache=True))

    run = BuscaTribunalRun.objects.create(
        criterio=entrada['criterio'], valor=entrada['valor'],
        valor_normalizado=entrada['normalizado'], tribunais=tribunais)
    iniciar(run)
    run.refresh_from_db()
    logger.info('busca (tela) criada', extra={'run': str(run.pk),
                                              'criterio': run.criterio,
                                              'tribunais': len(tribunais)})
    return JsonResponse(_resposta_do_run(run), status=202)


@login_required
@require_GET
def ler(request):
    """`?run=<uuid>` — o mesmo envelope da API, para a tela ir atualizando."""
    from django.core.exceptions import ValidationError

    run_id = (request.GET.get('run') or '').strip()
    try:
        run = BuscaTribunalRun.objects.get(pk=run_id)
    except (BuscaTribunalRun.DoesNotExist, ValidationError, ValueError, TypeError):
        return JsonResponse({'erro': 'busca_nao_encontrada'}, status=404)
    return JsonResponse(_resposta_do_run(run))
