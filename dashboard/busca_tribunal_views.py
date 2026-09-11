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
from enrichers.busca.base import OAB, ROTULOS
from enrichers.busca.entrada import UFS, EntradaInvalida, validar
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
        # o seletor de UF só aparece no critério OAB; a lista vem do BACKEND
        # para não haver duas fontes de verdade sobre quais UFs existem
        'ufs': UFS,
        'criterio_oab': OAB,
        'abrir_url': reverse('dashboard:processo-por-cnj', args=['CNJ']),
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
        entrada = validar(corpo.get('criterio'), corpo.get('valor'),
                          corpo.get('uf') or '')
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


@login_required
@never_cache
def processo_por_cnj(request, cnj: str):
    """Abre o processo pelo NÚMERO, venha ele do acervo ou do tribunal.

    A listagem da busca só tem o CNJ — o `processo-detail` quer o pk. Sem esta
    ponte, cada linha do resultado era um beco: o número na tela e nenhum jeito
    de chegar nos autos sem copiar, colar e procurar em outra tela.

    Dois caminhos, e a diferença entre eles é DITA, não escondida:

    1. **já no acervo** → 302 para a ficha completa, com partes e movimentações;
    2. **ainda não** → enfileira a hidratação e mostra a página de espera, com
       o link para a fonte pública ao lado. Não existe "abrir ao vivo" de
       verdade: trazer um processo é uma requisição ao tribunal, que leva
       segundos e pode falhar. Fingir que a ficha está pronta e servir uma
       vazia seria o `exists` do ES de novo — casca com cara de dado.
    """
    from django.shortcuts import redirect

    from search.busca_api import normalizar_cnj
    from tribunals.models import Process

    numero = normalizar_cnj(cnj) or (cnj or '').strip()
    proc = (Process.objects.filter(numero_cnj=numero)
            .values_list('id', flat=True).first())
    if proc:
        return redirect('dashboard:processo-detail', pk=proc)

    # Enfileira UMA vez por visita, e não a cada refresh da página de espera:
    # o `hidratar_achado` é idempotente, mas cada chamada é uma requisição ao
    # Datajud, cujo bucket de rate limit é global (ver `enrichers/busca/
    # ingestao.py`). Quem segura o refresh é a chave no cache.
    from django.core.cache import cache

    chave = f'busca:hidratando:{numero}'
    pedido = False
    if numero and not cache.get(chave):
        try:
            import django_rq

            from enrichers.busca.jobs import hidratar_achado
            django_rq.get_queue('busca_hidratacao').enqueue(
                hidratar_achado, numero, job_timeout=300)
            cache.set(chave, True, 120)
            pedido = True
        except Exception:  # noqa: BLE001 — sem fila, a página ainda serve
            logger.exception('busca: não consegui enfileirar a hidratação',
                             extra={'cnj': numero})

    return render(request, 'dashboard/processo_por_cnj.html', {
        'numero_cnj': numero,
        'url_fonte': (request.GET.get('fonte') or '').strip(),
        'tribunal': (request.GET.get('tribunal') or '').strip(),
        'pedido_agora': pedido,
    })
