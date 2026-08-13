"""Endpoints JSON da tela de Busca de Processos.

Mesmo padrão do `overview_views`: a view só faz parse → serviço → `JsonResponse`.
TODA a lógica (query, normalização de máscara, cobertura, avisos, contrato JSON)
mora em `search/busca_ui.py`, que por sua vez reusa `search/busca_api.py` — o
mesmo serviço que serve a API externa `/api/v1/busca/*`. Nada de query ES aqui.

Login-gated: é acervo de negócio. `@require_GET` porque busca é leitura.
Erros: 400 param inválido (CNJ malformado, CPF com DV errado, cursor forjado),
503 ES fora. Nunca 500 cru.
"""
import hashlib
import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from search import busca_ui
from search.busca_api import BuscaParamError

logger = logging.getLogger('voyager.dashboard.busca')

#: mensagem humana por código de erro — a tela precisa dizer COISAS DIFERENTES
#: pra "CPF inválido" e pra "não encontrado", e o texto é responsabilidade do
#: backend (é ele que sabe por que recusou)
MENSAGENS_ERRO = {
    'cnj_invalido': 'Número CNJ inválido: esperado 20 dígitos '
                    '(NNNNNNN-DD.AAAA.J.TR.OOOO).',
    'documento_invalido': 'Documento inválido: CPF tem 11 dígitos, CNPJ tem 14 '
                          '(ou 8, se for só a raiz).',
    'cpf_dv_invalido': 'CPF inválido — o dígito verificador não fecha. Confira '
                       'os números digitados.',
    'cnpj_dv_invalido': 'CNPJ inválido — o dígito verificador não fecha. Confira '
                        'os números digitados.',
    'cursor_invalido': 'Paginação expirada ou corrompida. Refaça a busca.',
}


def _json(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={'default': str})


def _erro(codigo, status=400):
    return _json({'erro': codigo,
                  'mensagem': MENSAGENS_ERRO.get(codigo, codigo)}, status=status)


def _chave_cache(prefixo, request):
    bruto = request.META.get('QUERY_STRING', '')
    return f'{prefixo}:' + hashlib.sha1(bruto.encode('utf-8')).hexdigest()[:20]


@login_required
@require_GET
def busca_processos(request):
    """GET /dashboard/api/busca/processos/ — busca de processos pra tela.

    Contrato completo (params e shape da resposta) no topo de `search/busca_ui.py`.
    Cache de `BUSCA_TTL`s pela querystring inteira: paginar pra frente e voltar,
    ou dar F5, não deve custar duas varreduras no ES que serve a dashboard.
    """
    chave = _chave_cache('busca_proc', request)
    try:
        cacheado = cache.get(chave)
    except Exception:                          # Redis fora não derruba a busca
        cacheado = None
    if cacheado is not None:
        return _json({**cacheado, 'cache': True})

    try:
        payload = busca_ui.buscar_da_querystring(request.GET)
    except BuscaParamError as e:
        return _erro(str(e))
    except Exception:
        logger.exception('busca_processos: falha ao consultar o índice',
                         extra={'qs': request.META.get('QUERY_STRING', '')[:500]})
        return _json({'erro': 'elasticsearch_indisponivel',
                      'mensagem': 'O índice de busca não respondeu. Tente de novo '
                                  'em instantes.'}, status=503)

    try:
        cache.set(chave, payload, busca_ui.BUSCA_TTL)
    except Exception:
        pass                                   # cache é otimização, não requisito
    return _json({**payload, 'cache': False})


@login_required
@require_GET
def busca_varas(request):
    """GET /dashboard/api/busca/varas/?q=&n= — autocomplete de vara/órgão julgador.

    Existe porque `orgao_julgador`/`juizo` são keyword crua com 29.488 grafias
    incompatíveis entre tribunais: sem escolher da lista, o usuário chuta. Cada
    item vem com o nº de processos, então dá pra ver que "5ª Vara Federal de
    Campinas" tem 19.382 e a homônima de outro tribunal, 12.
    """
    chave = _chave_cache('busca_varas', request)
    try:
        cacheado = cache.get(chave)
    except Exception:
        cacheado = None
    if cacheado is not None:
        return _json({**cacheado, 'cache': True})

    try:
        payload = busca_ui.sugerir_varas(request.GET.get('q'),
                                         request.GET.get('n'))
    except (TypeError, ValueError):
        return _json({'erro': 'parametro_invalido',
                      'mensagem': 'n deve ser um número.'}, status=400)
    except Exception:
        logger.exception('busca_varas: falha ao consultar o índice',
                         extra={'q': (request.GET.get('q') or '')[:120]})
        return _json({'erro': 'elasticsearch_indisponivel',
                      'mensagem': 'O índice de busca não respondeu.'}, status=503)

    if payload.get('buscou'):
        try:
            cache.set(chave, payload, busca_ui.VARAS_TTL)
        except Exception:
            pass
    return _json({**payload, 'cache': False})
